"""
API endpoint for bulk prerequisite operations on course subsections.
"""
import logging
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from django.http import Http404

from openedx.core.lib.api.view_utils import DeveloperErrorViewMixin, view_auth_classes
from cms.djangoapps.contentstore.api import course_author_access_required
from common.djangoapps.util.json_request import expect_json_in_class_view
from xmodule.modulestore.django import modulestore
from openedx.core.lib.gating import api as gating_api
from opaque_keys.edx.keys import CourseKey
import cms.djangoapps.contentstore.toggles as contentstore_toggles


log = logging.getLogger(__name__)


def auto_update_prerequisites(course_key, min_score='', min_completion='100'):
    """
    Utility function to automatically update prerequisites for all subsections in a course.

    This function can be called from various places to keep prerequisites in sync with
    the course structure.

    Args:
        course_key: The course key (can be string or CourseKey object)
        min_score: Minimum score percentage (default: '' - no score requirement, only completion)
        min_completion: Minimum completion percentage (default: '100' - must complete all content)

    Returns:
        tuple: (success: bool, message: str, details: list)
    """
    try:
        log.info(f"[AUTO-PREREQUISITES] Starting auto-update for course: {course_key}")

        # Parse course key if it's a string
        if isinstance(course_key, str):
            course_key = CourseKey.from_string(course_key)

        # Get the course from modulestore
        store = modulestore()
        course = store.get_course(course_key)

        if not course:
            log.error(f"[AUTO-PREREQUISITES] Course not found: {course_key}")
            return False, 'Course not found', []

        # Check if subsection gating is enabled
        gating_enabled = getattr(course, 'enable_subsection_gating', False)
        log.info(f"[AUTO-PREREQUISITES] Gating enabled: {gating_enabled} for course: {course_key}")

        if not gating_enabled:
            log.info(f"[AUTO-PREREQUISITES] Skipping - gating disabled for course: {course_key}")
            return False, 'Subsection gating is not enabled', []

        # Get all subsections (sequential blocks) in the course in order
        subsections = []
        for chapter in course.get_children():
            if chapter.category == 'chapter':
                for subsection in chapter.get_children():
                    if subsection.category == 'sequential':
                        subsections.append(subsection)

        log.info(f"[AUTO-PREREQUISITES] Found {len(subsections)} subsections in course: {course_key}")

        if not subsections:
            return True, 'No subsections found in the course', []

        # Process each subsection
        details = []
        previous_subsection = None

        for idx, subsection in enumerate(subsections, 1):
            try:
                # Mark this subsection as a prerequisite
                gating_api.add_prerequisite(course_key, subsection.location)

                prereq_usage_key = None

                # If this is not the first subsection, set the previous subsection as prerequisite
                if previous_subsection:
                    prereq_usage_key = str(previous_subsection.location)
                    log.info(f"[AUTO-PREREQUISITES] [{idx}/{len(subsections)}] '{subsection.display_name}' requires '{previous_subsection.display_name}'")
                    gating_api.set_required_content(
                        course_key,
                        subsection.location,
                        prereq_usage_key,
                        str(min_score),
                        str(min_completion)
                    )
                else:
                    log.info(f"[AUTO-PREREQUISITES] [{idx}/{len(subsections)}] '{subsection.display_name}' - first subsection, no prereq")

                # Handle empty string for min_score and min_completion
                prereq_min_score = None
                prereq_min_completion = None
                if prereq_usage_key:
                    try:
                        prereq_min_score = int(min_score) if min_score else 0
                    except (ValueError, TypeError):
                        prereq_min_score = 0
                    try:
                        prereq_min_completion = int(min_completion) if min_completion else 0
                    except (ValueError, TypeError):
                        prereq_min_completion = 0

                details.append({
                    'usage_key': str(subsection.location),
                    'display_name': subsection.display_name,
                    'is_prereq': True,
                    'prereq_usage_key': prereq_usage_key,
                    'prereq_min_score': prereq_min_score,
                    'prereq_min_completion': prereq_min_completion,
                })

            except Exception as e:
                log.error(
                    f"[AUTO-PREREQUISITES] Error for subsection {subsection.location}: {str(e)}"
                )
                details.append({
                    'usage_key': str(subsection.location),
                    'display_name': subsection.display_name,
                    'error': str(e)
                })
            finally:
                # IMPORTANT: Always update previous_subsection, even if there was an error
                # This ensures sequential chaining: Lesson 2→1, Lesson 3→2, Lesson 4→3, etc.
                previous_subsection = subsection

        log.info(f"[AUTO-PREREQUISITES] ✓ Completed! Processed {len(subsections)} subsections in course: {course_key}")
        return True, f'Prerequisites set successfully for {len(subsections)} subsections', details

    except Exception as e:
        log.error(f"[AUTO-PREREQUISITES] ✗ Error for course {course_key}: {str(e)}", exc_info=True)
        return False, str(e), []


@view_auth_classes()
class AutoPrerequisitesView(DeveloperErrorViewMixin, APIView):
    """
    API endpoint to automatically set prerequisites for all subsections in a course.

    POST /api/contentstore/v0/prerequisites/{course_id}/auto

    This endpoint will:
    1. Set isPrereq=true for all subsections in the course
    2. Set each subsection's prerequisite to the previous subsection (based on order)
    3. Optionally set min_score and min_completion requirements (default: no score requirement, 100% completion)

    Request body (optional):
    {
        "min_score": "",           # Default: "" (no score requirement, only completion matters)
        "min_completion": 100      # Default: 100 (must complete 100%)
    }

    Response:
    {
        "success": true,
        "message": "Prerequisites set successfully",
        "subsections_processed": 10,
        "details": [
            {
                "usage_key": "block-v1:...",
                "display_name": "Subsection 1",
                "is_prereq": true,
                "prereq_usage_key": null
            },
            ...
        ]
    }
    """

    def dispatch(self, request, *args, **kwargs):
        """Check if content api is enabled."""
        if not contentstore_toggles.use_studio_content_api():
            raise Http404
        return super().dispatch(request, *args, **kwargs)

    @course_author_access_required
    @expect_json_in_class_view
    def post(self, request, course_key):
        """
        Auto-set prerequisites for all subsections in the course.
        """
        try:
            # Parse request data
            request_data = request.data if hasattr(request, 'data') else {}
            min_score = str(request_data.get('min_score', ''))
            min_completion = str(request_data.get('min_completion', '100'))

            # Call the utility function
            success, message, details = auto_update_prerequisites(
                course_key,
                min_score=min_score,
                min_completion=min_completion
            )

            if not success:
                # Determine appropriate error status
                if 'not found' in message.lower():
                    error_status = status.HTTP_404_NOT_FOUND
                elif 'not enabled' in message.lower():
                    error_status = status.HTTP_400_BAD_REQUEST
                    message = 'Please enable subsection gating in advanced settings first'
                else:
                    error_status = status.HTTP_500_INTERNAL_SERVER_ERROR

                return Response(
                    {
                        'error': message,
                        'message': message
                    },
                    status=error_status
                )

            return Response(
                {
                    'success': True,
                    'message': message,
                    'subsections_processed': len(details),
                    'details': details
                },
                status=status.HTTP_200_OK
            )

        except Exception as e:
            log.error(f"Error in auto-prerequisites endpoint: {str(e)}", exc_info=True)
            return Response(
                {
                    'error': 'An error occurred while setting prerequisites',
                    'message': str(e)
                },
                status=status.HTTP_500_INTERNAL_SERVER_ERROR
            )
