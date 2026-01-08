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
    Automatically set prerequisites based on unskippable_unit flags.

    When a subsection has unskippable_unit=True, it becomes a checkpoint.
    All subsections after the checkpoint will have it as a prerequisite.

    Example:
        Subsections: 1, 2, 3, 4 ,
        If subsection 2 has unskippable_unit=True:
        - Subsection 3 gets prerequisite: subsection 2
        - Subsection 4 gets prerequisite: subsection 2

    Args:
        course_key: The course key (can be string or CourseKey object)
        min_score: Minimum score percentage (default: '', which means no minimum score)
        min_completion: Minimum completion percentage (default: '100')

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
        gating_enabled = getattr(course, 'enable_subsection_gating', True)
        log.info(f"[AUTO-PREREQUISITES] Gating enabled: {gating_enabled} for course: {course_key}")

        if not gating_enabled:
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

        # Find all checkpoints (subsections with unskippable_unit=True)
        checkpoints = []
        for idx, subsection in enumerate(subsections):
            unskippable = getattr(subsection, 'unskippable_unit', False)
            if unskippable:
                checkpoints.append((idx, subsection))
                log.info(
                    f"[AUTO-PREREQUISITES] Checkpoint found at position {idx + 1}: "
                    f"'{subsection.display_name}' ({subsection.location})"
                )

        # First pass: Create milestones for all checkpoints
        for idx, checkpoint in checkpoints:
            try:
                # Add prerequisite (creates milestone if it doesn't exist)
                gating_api.add_prerequisite(course_key, checkpoint.location)
                log.info(
                    f"[AUTO-PREREQUISITES] Created prerequisite milestone for '{checkpoint.display_name}'"
                )
            except Exception as e:
                log.error(
                    f"[AUTO-PREREQUISITES] Error creating prerequisite milestone for "
                    f"'{checkpoint.display_name}': {str(e)}"
                )

        if not checkpoints:
            # Clear all prerequisites if no checkpoints
            log.info("[AUTO-PREREQUISITES] No checkpoints found. Clearing all prerequisites.")
            for subsection in subsections:
                try:
                    gating_api.set_required_content(
                        course_key,
                        subsection.location,
                        None,
                        None,
                        None
                    )
                except Exception as e:
                    log.debug(f"[AUTO-PREREQUISITES] Error clearing prerequisite: {e}")

            return True, 'No checkpoints found. All prerequisites cleared.', []

        # Apply prerequisites: each subsection gets the most recent checkpoint before it
        details = []
        prerequisites_set = 0

        # Parse min_score and min_completion
        try:
            min_score_int = int(min_score) if min_score else 0
        except (ValueError, TypeError):
            min_score_int = 0

        try:
            min_completion_int = int(min_completion) if min_completion else 100
        except (ValueError, TypeError):
            min_completion_int = 100

        for idx, subsection in enumerate(subsections):
            unskippable = getattr(subsection, 'unskippable_unit', False)

            # Find the most recent checkpoint before this subsection
            active_checkpoint = None
            for checkpoint_idx, checkpoint in checkpoints:
                if checkpoint_idx < idx:
                    active_checkpoint = checkpoint
                else:
                    break

            # Set prerequisite if there's an active checkpoint
            if active_checkpoint and not unskippable:
                try:
                    # Ensure the prerequisite milestone exists before setting it
                    prereq_key = active_checkpoint.location

                    # Verify milestone exists
                    from milestones import api as milestones_api
                    milestone_namespace = f"{prereq_key}{'.gating'}"
                    milestones = milestones_api.get_milestones(milestone_namespace)

                    if not milestones:
                        log.warning(
                            f"[AUTO-PREREQUISITES] Milestone not found for '{active_checkpoint.display_name}', "
                            f"creating it now..."
                        )
                        gating_api.add_prerequisite(course_key, prereq_key)

                    # Now set the required content
                    gating_api.set_required_content(
                        course_key,
                        subsection.location,
                        prereq_key,  # Pass UsageKey directly, not string
                        str(min_score_int),
                        str(min_completion_int)
                    )
                    prerequisites_set += 1
                    log.info(
                        f"[AUTO-PREREQUISITES] Set prerequisite: '{subsection.display_name}' "
                        f"requires '{active_checkpoint.display_name}' "
                        f"(score>={min_score_int}%, completion>={min_completion_int}%)"
                    )
                    details.append({
                        'usage_key': str(subsection.location),
                        'display_name': subsection.display_name,
                        'position': idx + 1,
                        'unskippable_unit': unskippable,
                        'prerequisite': str(active_checkpoint.location),
                        'prerequisite_name': active_checkpoint.display_name,
                    })
                except Exception as e:
                    log.error(
                        f"[AUTO-PREREQUISITES] Error setting prerequisite for "
                        f"'{subsection.display_name}': {str(e)}",
                        exc_info=True
                    )
                    details.append({
                        'usage_key': str(subsection.location),
                        'display_name': subsection.display_name,
                        'position': idx + 1,
                        'error': str(e)
                    })
            else:
                # Clear prerequisite if no active checkpoint or is itself a checkpoint
                if not unskippable:
                    try:
                        gating_api.set_required_content(
                            course_key,
                            subsection.location,
                            None,
                            None,
                            None
                        )
                    except Exception as e:
                        log.debug(f"[AUTO-PREREQUISITES] Error clearing prerequisite: {e}")

                details.append({
                    'usage_key': str(subsection.location),
                    'display_name': subsection.display_name,
                    'position': idx + 1,
                    'unskippable_unit': unskippable,
                    'prerequisite': None,
                })

        message = (
            f'Prerequisites updated successfully: {len(checkpoints)} checkpoints found, '
            f'{prerequisites_set} prerequisites set.'
        )

        log.info(f"[AUTO-PREREQUISITES] ✓ {message}")
        return True, message, details

    except Exception as e:
        log.error(f"[AUTO-PREREQUISITES] ✗ Error for course {course_key}: {str(e)}", exc_info=True)
        return False, str(e), []


@view_auth_classes()
class AutoPrerequisitesView(DeveloperErrorViewMixin, APIView):
    """
    API endpoint to automatically set prerequisites based on unskippable_unit flags.

    POST /api/contentstore/v0/prerequisites/{course_id}/auto

    How it works:
    - Subsections with unskippable_unit=True become checkpoints
    - All subsections after a checkpoint get that checkpoint as a prerequisite
    - Students must complete checkpoints before accessing later content

    Example:
        Subsections: 1, 2, 3, 4
        If subsection 2 has unskippable_unit=True:
        - Subsection 3 requires subsection 2
        - Subsection 4 requires subsection 2

    Request body (optional):
    {
        "min_score": "",           # Minimum score % (default: 0, no minimum)
        "min_completion": 100      # Minimum completion % (default: 100)
    }

    Response:
    {
        "success": true,
        "message": "Prerequisites updated successfully: 2 checkpoints found, 5 prerequisites set",
        "subsections_processed": 10,
        "details": [
            {
                "usage_key": "block-v1:...",
                "display_name": "Subsection 1",
                "position": 1,
                "unskippable_unit": false,
                "prerequisite": null
            },
            {
                "usage_key": "block-v1:...",
                "display_name": "Subsection 2",
                "position": 2,
                "unskippable_unit": true,
                "prerequisite": null
            },
            {
                "usage_key": "block-v1:...",
                "display_name": "Subsection 3",
                "position": 3,
                "unskippable_unit": false,
                "prerequisite": "block-v1:...",
                "prerequisite_name": "Subsection 2"
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
