"""
Networked custom API views for course listing.

Provides a single endpoint that combines CourseOverview display data with
enrollment and progress data, eliminating the need for multiple API calls
from the Networked platform backend.
"""

import logging

from completion.exceptions import UnavailableCompletionData
from completion.utilities import get_key_to_last_completed_block
from django.core.paginator import EmptyPage, Paginator
from edx_rest_framework_extensions.auth.jwt.authentication import JwtAuthentication
from edx_rest_framework_extensions.auth.session.authentication import (
    SessionAuthenticationAllowInactiveUser,
)
from opaque_keys.edx.keys import CourseKey
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from common.djangoapps.student.models import CourseAccessRole, CourseEnrollment
from lms.djangoapps.courseware.courses import get_course_blocks_completion_summary
from openedx.core.djangoapps.content.course_overviews.models import CourseOverview
from openedx.core.djangoapps.models.course_details import CourseDetails
from openedx.core.lib.api.authentication import BearerAuthenticationAllowInactiveUser
from xmodule.modulestore.django import modulestore

logger = logging.getLogger(__name__)

DEFAULT_PAGE_SIZE = 20


class NetworkedCourseListView(APIView):
    """
    Combined course list endpoint for the Networked platform.

    Returns course display data (from CourseOverview) together with the
    requesting user's enrollment state, staff access, and progress — all
    in a single response.

    GET /api/learner_home/networked/courses/

    Query Parameters:
        org         (str,  required): Filter courses by organisation slug.
        page        (int,  optional): Page number, 1-indexed. Default: 1.
        page_size   (int,  optional): Items per page. Default: 20.
        search      (str,  optional): Case-insensitive substring match on
                                      course display name.
        enrolled_only (bool, optional): When "true", return only courses the
                                        user is actively enrolled in (My Courses
                                        tab). Default: false.
    """

    authentication_classes = (
        JwtAuthentication,
        BearerAuthenticationAllowInactiveUser,
        SessionAuthenticationAllowInactiveUser,
    )
    permission_classes = (IsAuthenticated,)

    def get(self, request):
        user = request.user
        org = request.query_params.get("org", "").strip()
        page = max(1, int(request.query_params.get("page", 1)))
        page_size = min(200, max(1, int(request.query_params.get("page_size", DEFAULT_PAGE_SIZE))))
        search = request.query_params.get("search", "").strip()
        # staff_only=true → My Courses (courses created/managed by this user)
        staff_only = request.query_params.get("staff_only", "false").lower() == "true"

        if not org:
            return Response({"error": "org parameter is required"}, status=400)

        if staff_only:
            course_overviews, total_count = self._get_staff_courses(user, org, search, page, page_size)
        else:
            course_overviews, total_count = self._get_all_courses(user, org, search, page, page_size)

        result_course_ids = [co.id for co in course_overviews]

        enrollments_map = self._get_enrollments_map(user, result_course_ids)

        enrolled_course_ids = list(enrollments_map.keys())

        # Bulk staff-role check (single DB query instead of per-course)
        staff_course_ids = self._get_staff_course_ids(user, result_course_ids)

        # Per-enrolled-course lookups (progress + isStarted)
        progress_map = self._get_progress_map(user, enrolled_course_ids)
        is_started_map = self._get_is_started_map(user, enrolled_course_ids)

        about_short_map = self._get_about_short_description_map(result_course_ids)

        results = []
        for co in course_overviews:
            is_staff = user.is_staff or (co.id in staff_course_ids)
            is_enrolled = co.id in enrollments_map
            progress = progress_map.get(co.id, {})

            results.append(
                {
                    "course_id": str(co.id),
                    "name": co.display_name or "",
                    "org": co.org,
                    "short_description": about_short_map.get(co.id, co.short_description or ""),
                    "image_url": co.course_image_url or "",
                    "catalog_visibility": co.catalog_visibility or "",
                    "is_staff": is_staff,
                    "is_enrolled": is_enrolled,
                    "is_started": is_started_map.get(co.id, False),
                    "progress": {
                        "complete_count": progress.get("complete_count", 0),
                        "incomplete_count": progress.get("incomplete_count", 0),
                        "locked_count": progress.get("locked_count", 0),
                    },
                }
            )

        return Response(
            {
                "count": total_count,
                "page": page,
                "page_size": page_size,
                "results": results,
            }
        )

    # ------------------------------------------------------------------ #
    # Private helpers
    # ------------------------------------------------------------------ #

    def _get_all_courses(self, user, org, search, page, page_size):
        """Return (course_overviews_page, total_count) for all courses in org."""
        qs = CourseOverview.objects.filter(org=org)
        if search:
            qs = qs.filter(display_name__icontains=search)
        qs = qs.order_by("-modified")

        total_count = qs.count()
        paginator = Paginator(qs, page_size)
        try:
            page_obj = paginator.page(page)
        except EmptyPage:
            return [], total_count

        return list(page_obj.object_list), total_count

    def _get_staff_courses(self, user, org, search, page, page_size):
        """
        Return (course_overviews_page, total_count) for courses where the user
        has a staff or instructor role (i.e. courses they created/manage).
        Global staff (user.is_staff) can see all courses in the org.
        """
        if user.is_staff:
            # Global staff — return all courses in org (same as All Courses)
            return self._get_all_courses(user, org, search, page, page_size)

        # Get course IDs where this user has a staff/instructor role
        staff_course_ids = CourseAccessRole.objects.filter(
            user=user,
            role__in=["staff", "instructor"],
        ).values_list("course_id", flat=True)

        qs = CourseOverview.objects.filter(org=org, id__in=staff_course_ids)
        if search:
            qs = qs.filter(display_name__icontains=search)
        qs = qs.order_by("-modified")

        total_count = qs.count()
        paginator = Paginator(qs, page_size)
        try:
            page_obj = paginator.page(page)
        except EmptyPage:
            return [], total_count

        return list(page_obj.object_list), total_count

    def _get_about_short_description_map(self, course_ids):
        """Return {course_id: short_description} using about/short_description as source of truth."""
        short_map = {}
        for course_id in course_ids:
            try:
                short_map[course_id] = CourseDetails.fetch_about_attribute(course_id, "short_description") or ""
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning(
                    "Error getting short_description for course=%s: %s",
                    course_id,
                    ex,
                )
                short_map[course_id] = ""
        return short_map

    def _get_enrollments_map(self, user, course_ids):
        """Return {course_id: CourseEnrollment} for active enrollments in given courses."""
        if not course_ids:
            return {}
        enrollments = CourseEnrollment.objects.filter(
            user=user,
            is_active=True,
            course_id__in=course_ids,
        )
        return {e.course_id: e for e in enrollments}

    def _get_staff_course_ids(self, user, course_ids):
        """
        Return set of course_ids where user has staff or instructor role.
        Uses a single bulk DB query instead of per-course checks.
        """
        if not course_ids or user.is_staff:
            # is_staff (global) means staff everywhere — no point querying roles
            return set()
        roles = CourseAccessRole.objects.filter(
            user=user,
            role__in=["staff", "instructor"],
            course_id__in=course_ids,
        ).values_list("course_id", flat=True)
        return set(roles)

    def _get_progress_map(self, user, course_ids):
        """Return {course_id: progress_dict} for each enrolled course."""
        progress_map = {}
        for course_id in course_ids:
            try:
                progress_map[course_id] = get_course_blocks_completion_summary(course_id, user)
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning(
                    "Error getting progress for user=%s course=%s: %s",
                    user,
                    course_id,
                    ex,
                )
        return progress_map

    def _get_is_started_map(self, user, course_ids):
        """Return {course_id: bool} indicating whether user has started each course."""
        is_started_map = {}
        for course_id in course_ids:
            try:
                block_key = get_key_to_last_completed_block(user, course_id)
                is_started_map[course_id] = bool(block_key)
            except UnavailableCompletionData:
                is_started_map[course_id] = False
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning(
                    "Error getting isStarted for user=%s course=%s: %s",
                    user,
                    course_id,
                    ex,
                )
                is_started_map[course_id] = False
        return is_started_map


class NetworkedCourseDetailView(APIView):
    """
    All-in-one course detail endpoint for the Networked platform.

    Replaces ALL 6 previous API calls (3 LMS + 3 Studio) with a single
    response by using the modulestore directly for course structure data.

    GET /api/learner_home/networked/course-detail/<course_id>/

    Path Parameters:
        course_id   (str): URL-encoded course key, e.g. course-v1:org+CS101+2024
    """

    authentication_classes = (
        JwtAuthentication,
        BearerAuthenticationAllowInactiveUser,
        SessionAuthenticationAllowInactiveUser,
    )
    permission_classes = (IsAuthenticated,)

    def get(self, request, course_id):
        user = request.user

        try:
            course_key = CourseKey.from_string(course_id)
        except Exception:  # pylint: disable=broad-except
            return Response({"error": f"Invalid course_id: {course_id}"}, status=400)

        # ── Course display data (CourseOverview — fast DB read) ──────────
        try:
            co = CourseOverview.get_from_id(course_key)
        except CourseOverview.DoesNotExist:
            return Response({"error": "Course not found"}, status=404)

        # ── Enrollment ───────────────────────────────────────────────────
        enrollment = CourseEnrollment.objects.filter(
            user=user, course_id=course_key, is_active=True
        ).first()
        is_enrolled = enrollment is not None

        # ── Staff access ─────────────────────────────────────────────────
        is_staff = user.is_staff or CourseAccessRole.objects.filter(
            user=user,
            role__in=["staff", "instructor"],
            course_id=course_key,
        ).exists()

        # ── Progress & isStarted (enrolled users only) ───────────────────
        progress = {}
        is_started = False
        if is_enrolled:
            try:
                progress = get_course_blocks_completion_summary(course_key, user)
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning("Error getting progress for user=%s course=%s: %s", user, course_id, ex)
            try:
                block_key = get_key_to_last_completed_block(user, course_key)
                is_started = bool(block_key)
            except UnavailableCompletionData:
                is_started = False
            except Exception as ex:  # pylint: disable=broad-except
                logger.warning("Error getting isStarted for user=%s course=%s: %s", user, course_id, ex)

        # ── Enroll count ─────────────────────────────────────────────────
        enroll_count = CourseEnrollment.objects.filter(
            course_id=course_key, is_active=True
        ).count()

        # ── Course structure + settings via modulestore ──────────────────
        # Replaces 3 Studio HTTP calls: getCourseStatus, getCourseAssignmentState, getCourseSetting
        advanced_modules = []
        course_status = "draft"
        sections_visible = 0
        subsections_visible = 0
        units_visible = 0

        try:
            store = modulestore()
            # depth=3 loads chapter → sequential → vertical (enough for status + structure)
            course_block = store.get_course(course_key, depth=3)
            if course_block:
                advanced_modules = list(getattr(course_block, "advanced_modules", []) or [])

                for section in course_block.get_children():
                    if getattr(section, "visible_to_staff_only", False):
                        continue
                    sections_visible += 1
                    for subsection in section.get_children():
                        if getattr(subsection, "visible_to_staff_only", False):
                            continue
                        subsections_visible += 1
                        for unit in subsection.get_children():
                            if not getattr(unit, "visible_to_staff_only", False):
                                units_visible += 1

                if sections_visible > 0 and subsections_visible > 0 and units_visible > 0:
                    course_status = "published"

        except Exception as ex:  # pylint: disable=broad-except
            logger.warning("Error reading modulestore for course=%s: %s", course_id, ex)

        # ── Media URIs ───────────────────────────────────────────────────
        course_image_uri = co.course_image_url or ""
        course_video_uri = getattr(co, "course_video_url", "") or ""
        # Use about/short_description as source of truth (same as Studio settings page).
        short_description = CourseDetails.fetch_about_attribute(course_key, "short_description") or ""
        if not short_description and course_block:
            short_description = getattr(course_block, "short_description", "") or ""
        if not short_description:
            short_description = co.short_description or ""

        overview = CourseDetails.fetch_about_attribute(course_key, "overview") or ""
        if not overview and course_block:
            overview = getattr(course_block, "overview", "") or ""

        return Response({
            # Core identifiers
            "id": str(co.id),
            "course_id": str(co.id),
            "name": co.display_name or "",
            "number": co.display_number_with_default or "",
            "org": co.org,
            "short_description": short_description,
            "overview": overview,
            "catalog_visibility": co.catalog_visibility or "",
            "media": {
                "course_image": {"uri": course_image_uri},
                "course_video": {"uri": course_video_uri},
            },
            "start": str(co.start) if co.start else None,
            "end": str(co.end) if co.end else None,
            "enrollment_start": str(co.enrollment_start) if co.enrollment_start else None,
            "enrollment_end": str(co.enrollment_end) if co.enrollment_end else None,
            "pacing": "self-paced" if co.self_paced else "instructor",

            # Enrollment & access
            "is_enrolled": is_enrolled,
            "enroll_count": enroll_count,
            "userData": {
                "isStaff": is_staff,
                "isEnrolled": is_enrolled,
                "isStarted": is_started,
            },

            # Progress
            "progress": {
                "complete_count": progress.get("complete_count", 0),
                "incomplete_count": progress.get("incomplete_count", 0),
                "locked_count": progress.get("locked_count", 0),
            },

            # Course status (replaces getCourseStatus / Studio quality API)
            "status": course_status,
            "sections": {"total_visible": sections_visible},
            "subsections": {"total_visible": subsections_visible},
            "units": {"total_visible": units_visible},

            # Advanced settings (replaces getCourseSetting / Studio contentstore API)
            "courseSettings": {
                "advanced_modules": {"value": advanced_modules},
            },
        })
