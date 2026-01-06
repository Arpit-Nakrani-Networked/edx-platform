"""
API for the gating djangoapp
"""

import json
import logging

from completion.models import BlockCompletion
from django.contrib.auth.models import User  # lint-amnesty, pylint: disable=imported-auth-user
from django.urls import reverse
from django.utils.translation import gettext as _
from milestones import api as milestones_api
from opaque_keys.edx.keys import UsageKey
from xblock.completable import XBlockCompletionMode as CompletionMode

from lms.djangoapps.course_blocks.api import get_course_blocks
from lms.djangoapps.courseware.access import _has_access_to_course
from lms.djangoapps.grades.api import SubsectionGradeFactory
from openedx.core.lib.gating.exceptions import GatingValidationError
from common.djangoapps.util import milestones_helpers
from xmodule.modulestore.django import modulestore  # lint-amnesty, pylint: disable=wrong-import-order
from xmodule.modulestore.exceptions import ItemNotFoundError  # lint-amnesty, pylint: disable=wrong-import-order

log = logging.getLogger(__name__)

# This is used to namespace gating-specific milestones
GATING_NAMESPACE_QUALIFIER = '.gating'


def _subsection_has_prevent_skip_video(subsection_usage_key, user):
    """
    Check if a subsection contains a video with prevent_skip_video=True

    Arguments:
        subsection_usage_key: UsageKey of the subsection
        user: The user object

    Returns:
        tuple: (has_prevent_skip_video, video_usage_key) - True if subsection has prevent_skip video, and the video key
    """
    try:
        store = modulestore()
        subsection = store.get_item(subsection_usage_key)
        log.info("[VIDEO-GATING] Checking subsection %s for prevent_skip videos", subsection_usage_key)

        # Recursively check all descendants for video blocks with prevent_skip_video=True
        def check_descendants(block, depth=0):
            # Skip None blocks
            if block is None:
                return False, None

            indent = "  " * depth
            block_category = getattr(block, 'category', None)
            block_location = getattr(block, 'location', 'unknown')

            log.debug("[VIDEO-GATING] %sChecking block %s, category=%s", indent, block_location, block_category)

            # Check if this block is a video with prevent_skip_video enabled
            if block_category == 'video':
                prevent_skip = getattr(block, 'prevent_skip_video', False)
                log.info("[VIDEO-GATING] %sFound video block %s, prevent_skip_video=%s",
                         indent, block_location, prevent_skip)
                if prevent_skip:
                    log.info("[VIDEO-GATING] ✓ Found prevent_skip video in subsection %s: %s",
                             subsection_usage_key, block_location)
                    return True, block_location

            # Recursively check children
            if hasattr(block, 'get_children'):
                try:
                    children = block.get_children()
                    for child in children:
                        if child is not None:  # Skip None children
                            has_video, video_key = check_descendants(child, depth + 1)
                            if has_video:
                                return True, video_key
                except Exception as e:
                    log.warning("[VIDEO-GATING] Error getting children for block %s: %s", block_location, e)

            return False, None

        result = check_descendants(subsection)
        if not result[0]:
            log.info("[VIDEO-GATING] No prevent_skip video found in subsection %s", subsection_usage_key)
        return result
    except Exception as e:
        log.error("Error checking for prevent_skip_video in subsection %s: %s", subsection_usage_key, e, exc_info=True)
        return False, None


def _get_previous_subsections_in_section(subsection_usage_key):
    """
    Get all subsections that come before the given subsection in the same section/chapter

    Arguments:
        subsection_usage_key: UsageKey of the current subsection

    Returns:
        list: List of (subsection_usage_key, index) tuples for previous subsections
    """
    try:
        store = modulestore()
        subsection = store.get_item(subsection_usage_key)

        # Get the parent section (chapter)
        section = subsection.get_parent()
        if not section:
            log.warning("[VIDEO-GATING] No parent section found for subsection %s", subsection_usage_key)
            return []

        # Get all children (subsections) of the section
        all_subsections = section.get_children()
        log.info("[VIDEO-GATING] Found %d subsections in section %s", len(all_subsections), section.location)

        # Find the index of the current subsection
        current_index = None
        for idx, child in enumerate(all_subsections):
            # Compare as strings to handle different UsageKey types
            if str(child.location) == str(subsection_usage_key):
                current_index = idx
                break

        if current_index is None:
            log.warning("[VIDEO-GATING] Could not find subsection %s in parent section", subsection_usage_key)
            return []

        log.info("[VIDEO-GATING] Subsection %s is at index %d, checking %d previous subsections",
                 subsection_usage_key, current_index, current_index)

        # Return all subsections before the current one
        previous_subsections = []
        for idx in range(current_index):
            previous_subsections.append((all_subsections[idx].location, idx))

        return previous_subsections
    except Exception as e:
        log.error("Error getting previous subsections for %s: %s", subsection_usage_key, e, exc_info=True)
        return []


def _is_video_completed(video_usage_key, user):
    """
    Check if user has completed watching a prevent_skip video

    Arguments:
        video_usage_key: UsageKey of the video
        user: The user object

    Returns:
        bool: True if the video is completed, False otherwise
    """
    try:
        course_key = video_usage_key.course_key
        course_block_completions = BlockCompletion.get_learning_context_completions(user, course_key)

        # Check if the video is marked as complete
        completion = course_block_completions.get(video_usage_key, 0)
        return completion >= 1.0
    except Exception as e:
        log.warning("Error checking video completion for %s, user %s: %s", video_usage_key, user.id, e)
        return False


def _has_video_based_prerequisite_requirement(content_id):
    """
    Check if a subsection might have video-based prerequisite requirements.
    Returns True if there are ANY prevent_skip videos in the course (checkpoint system is active).

    Arguments:
        content_id: UsageKey of the content being checked

    Returns:
        bool: True if checkpoint system is active (any prevent_skip videos exist in course)
    """
    try:
        course_key = content_id.course_key
        all_subsections = _get_all_subsections_in_course(course_key)

        # Check if ANY subsection in the course has a prevent_skip video
        for subsection_key, section_idx, subsection_idx in all_subsections:
            has_prevent_skip, _ = _subsection_has_prevent_skip_video(subsection_key, None)
            if has_prevent_skip:
                log.info("[VIDEO-GATING] Found prevent_skip video in course - checkpoint system is active")
                return True

        log.info("[VIDEO-GATING] No prevent_skip videos found in course - checkpoint system inactive")
        return False
    except Exception as e:
        log.warning("Error checking for video-based prerequisite requirement for %s: %s", content_id, e)
        return False


def _get_all_subsections_in_course(course_key):
    """
    Get ALL subsections in the entire course in sequential order.
    Always fetches fresh data from modulestore to handle content changes.

    Arguments:
        course_key: The course key

    Returns:
        list: List of (subsection_usage_key, section_index, subsection_index) tuples
    """
    try:
        # Always get fresh data from modulestore (no caching)
        store = modulestore()
        course = store.get_course(course_key)

        if not course:
            log.warning("[VIDEO-GATING] Could not find course %s", course_key)
            return []

        all_subsections = []

        # Get sections - handle None values
        try:
            sections = course.get_children()
        except Exception as e:
            log.error("[VIDEO-GATING] Error getting sections for course %s: %s", course_key, e)
            return []

        if not sections:
            log.info("[VIDEO-GATING] No sections found in course %s", course_key)
            return []

        for section_idx, section in enumerate(sections):
            # Skip None or deleted sections
            if section is None:
                log.warning("[VIDEO-GATING] Encountered None section at index %d", section_idx)
                continue

            try:
                subsections = section.get_children()
            except Exception as e:
                log.error("[VIDEO-GATING] Error getting subsections for section %s: %s",
                         getattr(section, 'location', 'unknown'), e)
                continue

            if not subsections:
                continue

            for subsection_idx, subsection in enumerate(subsections):
                # Skip None or deleted subsections
                if subsection is None:
                    log.warning("[VIDEO-GATING] Encountered None subsection at section %d, index %d",
                               section_idx, subsection_idx)
                    continue

                # Verify subsection has a valid location
                if not hasattr(subsection, 'location'):
                    log.warning("[VIDEO-GATING] Subsection at [%d.%d] has no location",
                               section_idx, subsection_idx)
                    continue

                all_subsections.append((subsection.location, section_idx, subsection_idx))

        log.info("[VIDEO-GATING] Found %d total subsections in course %s", len(all_subsections), course_key)
        return all_subsections

    except Exception as e:
        log.error("[VIDEO-GATING] Error getting all subsections for course %s: %s",
                 course_key, e, exc_info=True)
        return []


def _find_first_incomplete_video_checkpoint(course_key, user):
    """
    Find the first subsection in the course that contains an incomplete prevent_skip video.
    This subsection becomes the checkpoint - all subsections after it are locked.
    Handles deleted/missing content gracefully.

    Arguments:
        course_key: The course key
        user: The user object

    Returns:
        tuple: (checkpoint_subsection_key, video_key) or (None, None) if no checkpoint exists
    """
    try:
        log.info("[VIDEO-GATING] Scanning course %s for first incomplete video checkpoint for user %s",
                 course_key, user.id)

        # Get fresh list of all subsections
        all_subsections = _get_all_subsections_in_course(course_key)

        if not all_subsections:
            log.info("[VIDEO-GATING] No subsections found in course")
            return None, None

        for subsection_key, section_idx, subsection_idx in all_subsections:
            try:
                log.info("[VIDEO-GATING] Checking subsection [%d.%d]: %s",
                         section_idx, subsection_idx, subsection_key)

                # Check if this subsection still exists and has a prevent_skip video
                has_prevent_skip, video_key = _subsection_has_prevent_skip_video(subsection_key, user)

                if has_prevent_skip:
                    # Verify video still exists before checking completion
                    if video_key is None:
                        log.warning("[VIDEO-GATING] Subsection %s has prevent_skip flag but video_key is None",
                                   subsection_key)
                        continue

                    # Check if this video is completed
                    video_completed = _is_video_completed(video_key, user)
                    log.info("[VIDEO-GATING] Found prevent_skip video %s, completed=%s",
                             video_key, video_completed)

                    if not video_completed:
                        # This is our checkpoint - first incomplete prevent_skip video
                        log.info("[VIDEO-GATING] ✓ Found checkpoint at subsection %s (video: %s)",
                                 subsection_key, video_key)
                        return subsection_key, video_key
                    else:
                        log.info("[VIDEO-GATING] Video %s is complete, continuing scan...", video_key)

            except Exception as e:
                log.error("[VIDEO-GATING] Error checking subsection [%d.%d] %s: %s",
                         section_idx, subsection_idx, subsection_key, e)
                # Continue to next subsection even if this one errors
                continue

        # No incomplete checkpoints found
        log.info("[VIDEO-GATING] No incomplete video checkpoints found - all unlocked")
        return None, None

    except Exception as e:
        log.error("[VIDEO-GATING] Error finding video checkpoint for course %s, user %s: %s",
                  course_key, user.id, e, exc_info=True)
        return None, None


def _check_strict_subsection_order_prerequisites(content_id, user_id):
    """
    Check prerequisites based on strict subsection order.

    This function enforces sequential subsection completion:
    - Subsections must be completed in order
    - A subsection is locked until the previous subsection is 100% complete
    - Uses dynamic calculation based on current course structure (not stored IDs)
    - Handles content reordering gracefully

    Used for:
    - Rule 1: enable_subsection_gating = True
    - Rule 2: enable_subsection_gating = False AND minimum_time_on_unit > 0

    Arguments:
        content_id: UsageKey of the subsection being checked
        user_id: The user ID

    Returns:
        tuple: (prereq_met, prereq_meta_info) - Same format as compute_is_prereq_met
    """
    try:
        log.info("[STRICT-ORDER] Checking strict subsection order for subsection %s, user %s", content_id, user_id)
        user = User.objects.get(id=user_id)
        store = modulestore()
        course_key = content_id.course_key

        # Get all subsections in current course order (dynamic, not cached)
        all_subsections = _get_all_subsections_in_course(course_key)

        if not all_subsections:
            log.info("[STRICT-ORDER] No subsections found in course - unlocking")
            return True, {'url': None, 'display_name': None}

        # Find the current subsection's position
        current_position = None
        for idx, (subsection_key, section_idx, subsection_idx) in enumerate(all_subsections):
            if str(subsection_key) == str(content_id):
                current_position = idx
                log.info("[STRICT-ORDER] Found current subsection at position %d [section %d, subsection %d]",
                        idx, section_idx, subsection_idx)
                break

        # Handle case where current subsection was deleted/not found
        if current_position is None:
            log.warning("[STRICT-ORDER] Current subsection %s not found in course structure", content_id)
            return True, {'url': None, 'display_name': None}

        # First subsection is always unlocked
        if current_position == 0:
            log.info("[STRICT-ORDER] ✓ First subsection - unlocked")
            return True, {'url': None, 'display_name': None}

        # Get the previous subsection (dynamic based on current order)
        previous_position = current_position - 1
        previous_subsection_key, prev_section_idx, prev_subsection_idx = all_subsections[previous_position]

        log.info("[STRICT-ORDER] Previous subsection at position %d [section %d, subsection %d]: %s",
                previous_position, prev_section_idx, prev_subsection_idx, previous_subsection_key)

        # Check if previous subsection is completed
        previous_completion = get_subsection_completion_percentage(previous_subsection_key, user)

        log.info("[STRICT-ORDER] Previous subsection completion: %.1f%%", previous_completion)

        if previous_completion >= 100.0:
            # Previous subsection is complete - unlock current subsection
            log.info("[STRICT-ORDER] ✓ Previous subsection complete - unlocking current subsection")
            return True, {'url': None, 'display_name': None}
        else:
            # Previous subsection is incomplete - lock current subsection
            try:
                previous_subsection = store.get_item(previous_subsection_key)
                prereq_meta_info = {
                    'url': reverse('jump_to', kwargs={'course_id': course_key, 'location': previous_subsection_key}),
                    'display_name': getattr(previous_subsection, 'display_name', 'Previous Subsection'),
                    'id': str(previous_subsection_key)
                }
                log.info("[STRICT-ORDER] ✗ Previous subsection incomplete (%.1f%%) - locking current subsection",
                        previous_completion)
                return False, prereq_meta_info
            except Exception as e:
                log.error("[STRICT-ORDER] Error loading previous subsection %s: %s", previous_subsection_key, e)
                # If we can't load the previous subsection, don't block access
                return True, {'url': None, 'display_name': None}

    except Exception as e:
        log.error("[STRICT-ORDER] Error checking strict order prerequisites for %s, user %s: %s",
                 content_id, user_id, e, exc_info=True)
        # On error, don't block access (fail open for safety)
        return True, {'url': None, 'display_name': None}


def _check_video_based_prerequisites(content_id, user_id):
    """
    Check video-based prerequisites when enable_subsection_gating is False.

    Progressive checkpoint system:
    - Scan all subsections in course order (always fresh data)
    - Find the FIRST subsection with an incomplete prevent_skip video (the "checkpoint")
    - Lock ALL subsections that come after this checkpoint
    - Once checkpoint video is completed, scan forward to find the next checkpoint
    - Handles content deletion/reordering gracefully

    Arguments:
        content_id: UsageKey of the subsection being checked
        user_id: The user ID

    Returns:
        tuple: (prereq_met, prereq_meta_info) - Same format as compute_is_prereq_met
    """
    try:
        log.info("[VIDEO-GATING] Starting video-based prerequisite check for subsection %s, user %s",
                 content_id, user_id)
        user = User.objects.get(id=user_id)
        store = modulestore()
        course_key = content_id.course_key

        # Find the first incomplete video checkpoint in the course (always fresh scan)
        checkpoint_subsection_key, checkpoint_video_key = _find_first_incomplete_video_checkpoint(course_key, user)

        if checkpoint_subsection_key is None:
            # No checkpoints exist - all subsections are unlocked
            log.info("[VIDEO-GATING] ✓ No active checkpoint - subsection %s is unlocked", content_id)
            return True, {'url': None, 'display_name': None}

        # Get fresh list of all subsections to determine positions
        all_subsections = _get_all_subsections_in_course(course_key)

        if not all_subsections:
            log.warning("[VIDEO-GATING] No subsections found in course")
            return True, {'url': None, 'display_name': None}

        current_position = None
        checkpoint_position = None

        # Find positions of current subsection and checkpoint
        for idx, (subsection_key, section_idx, subsection_idx) in enumerate(all_subsections):
            if str(subsection_key) == str(content_id):
                current_position = idx
                log.info("[VIDEO-GATING] Found current subsection at position %d [%d.%d]",
                        idx, section_idx, subsection_idx)
            if str(subsection_key) == str(checkpoint_subsection_key):
                checkpoint_position = idx
                log.info("[VIDEO-GATING] Found checkpoint subsection at position %d [%d.%d]",
                        idx, section_idx, subsection_idx)

        # Handle case where current subsection was deleted/not found
        if current_position is None:
            log.warning("[VIDEO-GATING] Current subsection %s not found in course (may have been deleted)",
                       content_id)
            # Don't block access if we can't find the subsection
            return True, {'url': None, 'display_name': None}

        # Handle case where checkpoint subsection was deleted
        if checkpoint_position is None:
            log.warning("[VIDEO-GATING] Checkpoint subsection %s not found in course (may have been deleted)",
                       checkpoint_subsection_key)
            # Re-scan to find a new checkpoint
            log.info("[VIDEO-GATING] Re-scanning for new checkpoint...")
            return _check_video_based_prerequisites(content_id, user_id)

        log.info("[VIDEO-GATING] Positions - Current: %d, Checkpoint: %d", current_position, checkpoint_position)

        if current_position <= checkpoint_position:
            # Current subsection is AT or BEFORE the checkpoint - it's unlocked
            log.info("[VIDEO-GATING] ✓ Subsection %s is at/before checkpoint - unlocked", content_id)
            return True, {'url': None, 'display_name': None}
        else:
            # Current subsection is AFTER the checkpoint - it's LOCKED
            try:
                checkpoint_subsection = store.get_item(checkpoint_subsection_key)
                prereq_meta_info = {
                    'url': reverse('jump_to', kwargs={'course_id': course_key, 'location': checkpoint_subsection_key}),
                    'display_name': getattr(checkpoint_subsection, 'display_name', 'Required Content'),
                    'id': str(checkpoint_subsection_key)
                }
                log.info(
                    "[VIDEO-GATING] ✗ Subsection %s LOCKED - must complete video checkpoint in subsection %s",
                    content_id, checkpoint_subsection_key
                )
                return False, prereq_meta_info
            except Exception as e:
                log.error("[VIDEO-GATING] Error getting checkpoint subsection %s: %s",
                         checkpoint_subsection_key, e)
                # If we can't load the checkpoint, don't block access
                return True, {'url': None, 'display_name': None}

    except Exception as e:
        log.error("[VIDEO-GATING] Error checking video-based prerequisites for %s, user %s: %s",
                 content_id, user_id, e, exc_info=True)
        # On error, don't block access (fail open for safety)
        return True, {'url': None, 'display_name': None}


def _get_prerequisite_milestone(prereq_content_key):
    """
    Get gating milestone associated with the given content usage key.

    Arguments:
        prereq_content_key (str|UsageKey): The content usage key

    Returns:
        dict: Milestone dict
    """
    milestones = milestones_api.get_milestones("{usage_key}{qualifier}".format(
        usage_key=prereq_content_key,
        qualifier=GATING_NAMESPACE_QUALIFIER
    ))

    if not milestones:
        log.warning("Could not find gating milestone for prereq UsageKey %s", prereq_content_key)
        return None

    if len(milestones) > 1:
        # We should only ever have one gating milestone per UsageKey
        # Log a warning here and pick the first one
        log.warning("Multiple gating milestones found for prereq UsageKey %s", prereq_content_key)

    return milestones[0]


def _validate_min_score(min_score):
    """
    Validates the minimum score entered by the Studio user.

    Arguments:
        min_score (str|int): The minimum score to validate

    Returns:
        None

    Raises:
        GatingValidationError: If the minimum score is not valid
    """
    if min_score:
        message = _("%(min_score)s is not a valid grade percentage") % {'min_score': min_score}
        try:
            min_score = int(min_score)
        except ValueError:
            raise GatingValidationError(message)  # lint-amnesty, pylint: disable=raise-missing-from

        if min_score < 0 or min_score > 100:
            raise GatingValidationError(message)


def gating_enabled(default=None):
    """
    Decorator that checks the enable_subsection_gating course flag to
    see if the subsection gating feature is active for a given course.
    If not, calls to the decorated function return the specified default value.

    Arguments:
        default (ANY): The value to return if the enable_subsection_gating course flag is False

    Returns:
        ANY: The specified default value if the gating feature is off,
        otherwise the result of the decorated function
    """
    def wrap(f):  # pylint: disable=missing-docstring
        def function_wrapper(course, *args):
            if not course.enable_subsection_gating:
                return default
            return f(course, *args)
        return function_wrapper
    return wrap


def find_gating_milestones(course_key, content_key=None, relationship=None, user=None):
    """
    Finds gating milestone dicts related to the given supplied parameters.

    Arguments:
        course_key (str|CourseKey): The course key
        content_key (str|UsageKey): The content usage key
        relationship (str): The relationship type (e.g. 'requires')
        user (dict): The user dict (e.g. {'id': 4})

    Returns:
        list: A list of milestone dicts
    """
    return [
        m for m in milestones_api.get_course_content_milestones(course_key, content_key, relationship, user)
        if GATING_NAMESPACE_QUALIFIER in m.get('namespace')
    ]


def get_gating_milestone(course_key, content_key, relationship):
    """
    Gets a single gating milestone dict related to the given supplied parameters.

    Respects the priority rules:
    - Rule 1: enable_subsection_gating = True → use stored milestones (manual gating)
    - Rule 2: enable_subsection_gating = False AND minimum_time_on_unit > 0 → use strict order (pseudo-milestone)
    - Rule 3: enable_subsection_gating = False AND minimum_time_on_unit = 0 → use video-based (pseudo-milestone)

    Arguments:
        course_key (str|CourseKey): The course key
        content_key (str|UsageKey): The content usage key
        relationship (str): The relationship type (e.g. 'requires')

    Returns:
        dict or None: The gating milestone dict or None
    """
    # Check course settings
    store = modulestore()
    course = store.get_course(course_key)

    if course:
        enable_subsection_gating = getattr(course, 'enable_subsection_gating', False)
        minimum_time_on_unit = getattr(course, 'minimum_time_on_unit', 0)

        log.info("[PREREQ-MILESTONE] Course %s - enable_subsection_gating=%s, minimum_time_on_unit=%s, relationship=%s",
                 course_key, enable_subsection_gating, minimum_time_on_unit, relationship)

        # RULE 1: enable_subsection_gating = True → use stored milestones (will be handled below)
        if enable_subsection_gating:
            log.info("[PREREQ-MILESTONE] Rule 1: Using stored milestones (manual gating)")
            # Fall through to use stored milestones
        # RULE 2: enable_subsection_gating = False AND minimum_time_on_unit > 0 → strict order
        elif minimum_time_on_unit > 0:
            log.info("[PREREQ-MILESTONE] Rule 2: Using strict subsection order")
            # Only return pseudo-milestone for 'requires' relationship
            if relationship == 'requires':
                return {
                    'id': 'strict-order-prereq',
                    'namespace': 'strict-order-gating',
                    'strict_order': True  # Special flag to indicate strict order gating
                }
            return None
        # RULE 3: enable_subsection_gating = False AND minimum_time_on_unit = 0 → video-based
        else:
            log.info("[PREREQ-MILESTONE] Rule 3: Using video-based prerequisites")
            # Only check for 'requires' relationship (when checking if THIS content requires a prereq)
            if relationship == 'requires':
                has_video_prereq = _has_video_based_prerequisite_requirement(content_key)
                log.info("[PREREQ-MILESTONE] Video-based prerequisite requirement for %s: %s",
                         content_key, has_video_prereq)
                if has_video_prereq:
                    # Return a pseudo-milestone to indicate video-based prerequisite exists
                    log.info("[PREREQ-MILESTONE] Returning pseudo-milestone for video-based gating")
                    return {
                        'id': 'video-based-prereq',
                        'namespace': 'video-based-gating',
                        'video_based': True  # Special flag to indicate this is video-based gating
                    }
            # Otherwise return None (no prerequisite)
            log.info("[PREREQ-MILESTONE] No video-based prerequisites found, returning None")
            return None
    else:
        log.warning("[PREREQ-MILESTONE] Could not retrieve course for %s", course_key)

    # For Rule 1 (enable_subsection_gating = True), use stored milestones
    try:
        return find_gating_milestones(course_key, content_key, relationship)[0]
    except IndexError:
        return None


def get_prerequisites(course_key):
    """
    Find all the gating milestones associated with a course and the
    XBlock info associated with those gating milestones.

    Arguments:
        course_key (str|CourseKey): The course key

    Returns:
        list: A list of dicts containing the milestone and associated XBlock info
    """
    course_content_milestones = find_gating_milestones(course_key)

    milestones_by_block_id = {}
    block_ids = []
    for milestone in course_content_milestones:
        prereq_content_key = _get_gating_block_id(milestone)
        block_id = UsageKey.from_string(prereq_content_key).block_id
        block_ids.append(block_id)
        milestones_by_block_id[block_id] = milestone

    result = []
    for block in modulestore().get_items(course_key, qualifiers={'name': block_ids}):
        milestone = milestones_by_block_id.get(block.location.block_id)
        if milestone:
            milestone['block_display_name'] = block.display_name
            milestone['block_usage_key'] = str(block.location)
            result.append(milestone)

    return result


def add_prerequisite(course_key, prereq_content_key):
    """
    Creates a new Milestone and CourseContentMilestone indicating that
    the given course content fulfills a prerequisite for gating

    Arguments:
        course_key (str|CourseKey): The course key
        prereq_content_key (str|UsageKey): The prerequisite content usage key

    Returns:
        None
    """
    milestone = milestones_api.add_milestone(
        {
            'name': _('Gating milestone for {usage_key}').format(usage_key=str(prereq_content_key)),
            'namespace': "{usage_key}{qualifier}".format(
                usage_key=prereq_content_key,
                qualifier=GATING_NAMESPACE_QUALIFIER
            ),
            'description': _('System defined milestone'),
        },
        propagate=False
    )
    milestones_api.add_course_content_milestone(course_key, prereq_content_key, 'fulfills', milestone)


def remove_prerequisite(prereq_content_key):
    """
    Removes the Milestone and CourseContentMilestones related to the gating
    prerequisite which the given course content fulfills

    Arguments:
        prereq_content_key (str|UsageKey): The prerequisite content usage key

    Returns:
        None
    """
    milestones = milestones_api.get_milestones("{usage_key}{qualifier}".format(
        usage_key=prereq_content_key,
        qualifier=GATING_NAMESPACE_QUALIFIER
    ))
    for milestone in milestones:
        milestones_api.remove_milestone(milestone.get('id'))


def is_prerequisite(course_key, prereq_content_key):
    """
    Returns True if there is at least one CourseContentMilestone
    which the given course content fulfills

    Arguments:
        course_key (str|CourseKey): The course key
        prereq_content_key (str|UsageKey): The prerequisite content usage key

    Returns:
        bool: True if the course content fulfills a CourseContentMilestone, otherwise False
    """
    return get_gating_milestone(
        course_key,
        prereq_content_key,
        'fulfills'
    ) is not None


def set_required_content(course_key, gated_content_key, prereq_content_key, min_score='', min_completion=''):
    """
    Adds a `requires` milestone relationship for the given gated_content_key if a prerequisite
    prereq_content_key is provided. If prereq_content_key is None, removes the `requires`
    milestone relationship.

    Arguments:
        course_key (str|CourseKey): The course key
        gated_content_key (str|UsageKey): The gated content usage key
        prereq_content_key (str|UsageKey): The prerequisite content usage key
        min_score (str|int): The minimum score
        min_completion (str|int): The minimum completion percentage

    Returns:
        None
    """
    milestone = None
    for gating_milestone in find_gating_milestones(course_key, gated_content_key, 'requires'):
        if not prereq_content_key or prereq_content_key not in gating_milestone.get('namespace'):
            milestones_api.remove_course_content_milestone(course_key, gated_content_key, gating_milestone)
        else:
            milestone = gating_milestone

    if prereq_content_key:
        _validate_min_score(min_score)

        # Convert empty strings to '0' for storage
        # The milestones API will try to convert these to int, so we need valid string representations
        if min_score == '' or min_score is None:
            min_score = '0'
        if min_completion == '' or min_completion is None:
            min_completion = '0'

        requirements = {'min_score': min_score, 'min_completion': min_completion}
        if not milestone:
            milestone = _get_prerequisite_milestone(prereq_content_key)
        milestones_api.add_course_content_milestone(course_key, gated_content_key, 'requires', milestone, requirements)


def get_required_content(course_key, gated_content_key):
    """
    Returns the prerequisite content usage key, minimum score and minimum completion percentage needed for fulfillment
    of that prerequisite for the given gated_content_key.

    Args:
        course_key (str|CourseKey): The course key
        gated_content_key (str|UsageKey): The gated content usage key

    Returns:
        tuple: The prerequisite content usage key, minimum score and minimum completion percentage,
        (None, None, None) if the content is not gated
    """
    milestone = get_gating_milestone(course_key, gated_content_key, 'requires')
    if milestone:
        return (
            _get_gating_block_id(milestone),
            milestone.get('requirements', {}).get('min_score', None),
            milestone.get('requirements', {}).get('min_completion', None),
        )
    else:
        return None, None, None


@gating_enabled(default=[])
def get_gated_content(course, user):
    """
    Returns the unfulfilled gated content usage keys in the given course.

    Arguments:
        course (CourseBlock): The course
        user (User): The user

    Returns:
        list: The list of gated content usage keys for the given course
    """
    if _has_access_to_course(user, 'staff', course.id):
        return []
    else:
        # Get the unfulfilled gating milestones for this course, for this user
        return [
            m['content_id'] for m in find_gating_milestones(
                course.id,
                None,
                'requires',
                {'id': user.id}
            )
        ]


def is_gate_fulfilled(course_key, gating_content_key, user_id):
    """
    Determines if a prerequisite section specified by gating_content_key
    has any unfulfilled milestones.

    Respects the priority rules:
    - Rule 1: enable_subsection_gating = True → check stored milestones
    - Rule 2 & 3: enable_subsection_gating = False → check completion directly

    Arguments:
        course_key (CourseUsageLocator): Course locator
        gating_content_key (BlockUsageLocator): The locator for the section content
        user_id: The id of the user

    Returns:
        Returns True if section has no unfufilled milestones or is not a prerequisite.
        Returns False otherwise
    """
    # Check course settings
    store = modulestore()
    course = store.get_course(course_key)

    if not course:
        return True

    enable_subsection_gating = getattr(course, 'enable_subsection_gating', False)
    minimum_time_on_unit = getattr(course, 'minimum_time_on_unit', 0)

    # RULE 1: enable_subsection_gating = True → use stored milestones
    if enable_subsection_gating:
        gating_milestone = get_gating_milestone(course_key, gating_content_key, "fulfills")
        if not gating_milestone:
            return True

        unfulfilled_milestones = [
            m['content_id'] for m in find_gating_milestones(
                course_key,
                None,
                'requires',
                {'id': user_id}
            ) if m['namespace'] == gating_milestone['namespace']
        ]
        return not unfulfilled_milestones

    # RULE 2 & 3: enable_subsection_gating = False → check completion directly
    # For strict order and video-based, a gate is fulfilled when the subsection is 100% complete
    user = User.objects.get(id=user_id)
    completion = get_subsection_completion_percentage(gating_content_key, user)

    log.info("[PREREQ] is_gate_fulfilled for subsection %s, user %s: completion=%.1f%%",
             gating_content_key, user_id, completion)

    return completion >= 100.0


def compute_is_prereq_met(content_id, user_id, recalc_on_unmet=False):
    """
    Returns true if the prequiste has been met for a given milestone.
    Will recalculate the subsection grade if specified and prereq unmet

    Prerequisite logic follows strict priority rules:
    - Rule 1 (highest): If enable_subsection_gating = True → strict subsection order
    - Rule 2: If enable_subsection_gating = False AND minimum_time_on_unit > 0 → strict subsection order
    - Rule 3: If enable_subsection_gating = False AND minimum_time_on_unit = 0 → video-based prerequisites

    Arguments:
        content_id (BlockUsageLocator): BlockUsageLocator for the content
        user_id: The id of the user
        recalc_on_unmet: Recalculate the grade if prereq has not yet been met

    Returns:
        tuple: True|False,
        prereq_meta_info = { 'url': prereq_url|None, 'display_name': prereq_name|None}
    """
    course_key = content_id.course_key

    # Get course to check settings
    store = modulestore()
    course = store.get_course(course_key)

    log.info("[PREREQ] compute_is_prereq_met called for content_id=%s, user_id=%s", content_id, user_id)

    if not course:
        log.warning("[PREREQ] Could not retrieve course %s in compute_is_prereq_met", course_key)
        # Default to no prerequisites if course not found
        return True, {'url': None, 'display_name': None}

    # Get settings
    enable_subsection_gating = getattr(course, 'enable_subsection_gating', False)
    minimum_time_on_unit = getattr(course, 'minimum_time_on_unit', 0)

    log.info("[PREREQ] Course %s settings - enable_subsection_gating=%s, minimum_time_on_unit=%s",
             course_key, enable_subsection_gating, minimum_time_on_unit)

    # PRIORITY RULE 1: enable_subsection_gating = True → strict subsection order
    if enable_subsection_gating:
        log.info("[PREREQ] ✓ Rule 1 activated: enable_subsection_gating=True → Using strict subsection order")
        return _check_strict_subsection_order_prerequisites(content_id, user_id)

    # PRIORITY RULE 2: enable_subsection_gating = False AND minimum_time_on_unit > 0 → strict subsection order
    if minimum_time_on_unit > 0:
        log.info("[PREREQ] ✓ Rule 2 activated: enable_subsection_gating=False AND minimum_time_on_unit=%d → Using strict subsection order",
                 minimum_time_on_unit)
        return _check_strict_subsection_order_prerequisites(content_id, user_id)

    # PRIORITY RULE 3: enable_subsection_gating = False AND minimum_time_on_unit = 0 → video-based prerequisites
    log.info("[PREREQ] ✓ Rule 3 activated: enable_subsection_gating=False AND minimum_time_on_unit=0 → Using video-based prerequisites")
    return _check_video_based_prerequisites(content_id, user_id)


def update_milestone(milestone, usage_key, prereq_milestone, user, grade_percentage=None, completion_percentage=None):
    """
    Updates the milestone record based on evaluation of prerequisite met.

    Arguments:
        milestone: The gated milestone being evaluated
        usage_key: Usage key of the prerequisite subsection
        prereq_milestone: The gating milestone
        user: The user who has fulfilled milestone
        grade_percentage: Grade percentage of prerequisite subsection
        completion_percentage: Completion percentage of prerequisite subsection

    Returns:
        True if prerequisite has been met, False if not
    """
    min_score, min_completion = _get_minimum_required_percentage(milestone)
    if not grade_percentage:
        grade_percentage = get_subsection_grade_percentage(usage_key, user) if min_score > 0 else 0
    if not completion_percentage:
        completion_percentage = get_subsection_completion_percentage(usage_key, user) if min_completion > 0 else 0

    # Log prerequisite evaluation for debugging
    log.info(
        '[GATING] Evaluating prerequisite for user %s, subsection %s: '
        'grade=%.1f%% (required: %.1f%%), completion=%.1f%% (required: %.1f%%)',
        user.id, usage_key, grade_percentage, min_score, completion_percentage, min_completion
    )

    if grade_percentage >= min_score and completion_percentage >= min_completion:
        log.info('[GATING] ✓ Prerequisite MET for user %s, subsection %s', user.id, usage_key)
        milestones_helpers.add_user_milestone({'id': user.id}, prereq_milestone)
        return True
    else:
        log.info('[GATING] ✗ Prerequisite NOT MET for user %s, subsection %s', user.id, usage_key)
        milestones_helpers.remove_user_milestone({'id': user.id}, prereq_milestone)
        return False


def _get_gating_block_id(milestone):
    """
    Return the block id of the gating milestone
    """
    return milestone.get('namespace', '').replace(GATING_NAMESPACE_QUALIFIER, '')


def get_subsection_grade_percentage(subsection_usage_key, user):
    """
    Computes grade percentage for a subsection in a given course for a user

    Arguments:
        subsection_usage_key: key of subsection
        user: The user whose grade needs to be computed

    Returns:
        User's grade percentage for given subsection
    """
    try:
        subsection_structure = get_course_blocks(user, subsection_usage_key)
        if any(subsection_structure):
            subsection_grade_factory = SubsectionGradeFactory(user, course_structure=subsection_structure)
            if subsection_usage_key in subsection_structure:
                subsection_grade = subsection_grade_factory.update(subsection_structure[subsection_usage_key])
                return _get_subsection_percentage(subsection_grade)
    except ItemNotFoundError as err:
        log.warning("Could not find course_block for subsection=%s error=%s", subsection_usage_key, err)
    return 0.0


def get_subsection_completion_percentage(subsection_usage_key, user):
    """
    Computes completion percentage for a subsection in a given course for a user
    Arguments:
        subsection_usage_key: key of subsection
        user: The user whose completion percentage needs to be computed
    Returns:
        User's completion percentage for given subsection
    """
    subsection_completion_percentage = 0.0
    try:
        subsection_structure = get_course_blocks(user, subsection_usage_key)
        if any(subsection_structure):
            completable_blocks = []
            for block in subsection_structure:
                completion_mode = subsection_structure.get_xblock_field(
                    block, 'completion_mode'
                )

                #  always exclude html blocks (in addition to EXCLUDED blocks) for gating calculations
                #  See https://openedx.atlassian.net/browse/WL-1798
                if completion_mode not in (CompletionMode.AGGREGATOR, CompletionMode.EXCLUDED) \
                        and not block.block_type == 'html':
                    completable_blocks.append(block)

            if not completable_blocks:
                return 100
            subsection_completion_total = 0
            course_key = subsection_usage_key.course_key
            course_block_completions = BlockCompletion.get_learning_context_completions(user, course_key)
            for block in completable_blocks:
                if course_block_completions.get(block):
                    subsection_completion_total += course_block_completions.get(block)
            subsection_completion_percentage = min(
                100 * (subsection_completion_total / float(len(completable_blocks))), 100
            )

    except ItemNotFoundError as err:
        log.warning("Could not find course_block for subsection=%s error=%s", subsection_usage_key, err)

    return subsection_completion_percentage


def _get_minimum_required_percentage(milestone):
    """
    Returns the minimum score and minimum completion percentage requirement for the given milestone.
    """
    # Default minimum score and minimum completion percentage to 100
    min_score = 100
    min_completion = 100
    requirements = milestone.get('requirements')
    if requirements:
        # Handle min_score: empty string should mean 0 (no requirement), not 100
        min_score_raw = requirements.get('min_score')
        try:
            # If min_score is empty string, None, or 0, treat as 0 (no score requirement)
            if min_score_raw == '' or min_score_raw is None:
                min_score = 0
            else:
                min_score = int(min_score_raw)
        except (ValueError, TypeError):
            log.warning(
                'Gating: Failed to parse minimum score "%s" for gating milestone %s, defaulting to 100',
                min_score_raw,
                json.dumps(milestone)
            )
            min_score = 100

        # Handle min_completion: empty string or None should default to 0
        min_completion_raw = requirements.get('min_completion')
        try:
            if min_completion_raw == '' or min_completion_raw is None:
                min_completion = 0
            else:
                min_completion = int(min_completion_raw)
        except (ValueError, TypeError):
            log.warning(
                'Gating: Failed to parse minimum completion percentage "%s" for gating milestone %s, defaulting to 100',
                min_completion_raw,
                json.dumps(milestone)
            )
            min_completion = 100
    return min_score, min_completion


def _get_subsection_percentage(subsection_grade):
    """
    Returns the percentage value of the given subsection_grade.
    """
    return subsection_grade.percent_graded * 100.0
