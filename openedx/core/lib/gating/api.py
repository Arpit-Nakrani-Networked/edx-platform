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


def _subsection_has_unskippable_unit(subsection_usage_key):
    """
    Check if a subsection has unskippable_unit=True

    Arguments:
        subsection_usage_key: UsageKey of the subsection

    Returns:
        bool: True if subsection has unskippable_unit=True
    """
    try:
        store = modulestore()
        subsection = store.get_item(subsection_usage_key)
        unskippable = getattr(subsection, 'unskippable_unit', False)
        log.info("[UNSKIPPABLE-GATING] Subsection %s has unskippable_unit=%s", subsection_usage_key, unskippable)
        return unskippable
    except Exception as e:
        log.error("Error checking unskippable_unit for subsection %s: %s", subsection_usage_key, e, exc_info=True)
        return False


def _has_unskippable_prerequisite_requirement(content_id):
    """
    Check if a subsection might have unskippable_unit prerequisite requirements.
    Returns True if there are ANY unskippable_unit subsections in the course.

    Arguments:
        content_id: UsageKey of the content being checked

    Returns:
        bool: True if checkpoint system is active (any unskippable_unit subsections exist in course)
    """
    try:
        course_key = content_id.course_key
        all_subsections = _get_all_subsections_in_course(course_key)

        # Check if ANY subsection in the course has unskippable_unit=True
        for subsection_key, section_idx, subsection_idx in all_subsections:
            if _subsection_has_unskippable_unit(subsection_key):
                log.info("[UNSKIPPABLE-GATING] Found unskippable_unit in course - checkpoint system is active")
                return True

        log.info("[UNSKIPPABLE-GATING] No unskippable_unit subsections found in course - checkpoint system inactive")
        return False
    except Exception as e:
        log.warning("Error checking for unskippable_unit prerequisite requirement for %s: %s", content_id, e)
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
            log.warning("[UNSKIPPABLE-GATING] Could not find course %s", course_key)
            return []

        all_subsections = []

        # Get sections - handle None values
        try:
            sections = course.get_children()
        except Exception as e:
            log.error("[UNSKIPPABLE-GATING] Error getting sections for course %s: %s", course_key, e)
            return []

        if not sections:
            log.info("[UNSKIPPABLE-GATING] No sections found in course %s", course_key)
            return []

        for section_idx, section in enumerate(sections):
            # Skip None or deleted sections
            if section is None:
                log.warning("[UNSKIPPABLE-GATING] Encountered None section at index %d", section_idx)
                continue

            try:
                subsections = section.get_children()
            except Exception as e:
                log.error("[UNSKIPPABLE-GATING] Error getting subsections for section %s: %s",
                         getattr(section, 'location', 'unknown'), e)
                continue

            if not subsections:
                continue

            for subsection_idx, subsection in enumerate(subsections):
                # Skip None or deleted subsections
                if subsection is None:
                    log.warning("[UNSKIPPABLE-GATING] Encountered None subsection at section %d, index %d",
                               section_idx, subsection_idx)
                    continue

                # Verify subsection has a valid location
                if not hasattr(subsection, 'location'):
                    log.warning("[UNSKIPPABLE-GATING] Subsection at [%d.%d] has no location",
                               section_idx, subsection_idx)
                    continue

                all_subsections.append((subsection.location, section_idx, subsection_idx))

        log.info("[UNSKIPPABLE-GATING] Found %d total subsections in course %s", len(all_subsections), course_key)
        return all_subsections

    except Exception as e:
        log.error("[UNSKIPPABLE-GATING] Error getting all subsections for course %s: %s",
                 course_key, e, exc_info=True)
        return []


def _check_unskippable_unit_prerequisites(content_id, user_id):
    """
    Check prerequisites based on unskippable_unit checkpoints.

    Progressive checkpoint system:
    - Each subsection's prerequisite is the most recent unskippable checkpoint before it
    - For consecutive unskippable subsections, each depends on the previous one
    - Handles content deletion/reordering gracefully

    Example:
        Subsections: 1, 2 (unskippable), 3 (unskippable), 4
        - Subsection 1: No prerequisite
        - Subsection 2: No prerequisite (first checkpoint)
        - Subsection 3: Prerequisite is subsection 2 (previous unskippable)
        - Subsection 4: Prerequisite is subsection 3 (most recent unskippable)

    Arguments:
        content_id: UsageKey of the subsection being checked
        user_id: The user ID

    Returns:
        tuple: (prereq_met, prereq_meta_info) - Same format as compute_is_prereq_met
    """
    try:
        log.info("[UNSKIPPABLE-GATING] Starting unskippable_unit prerequisite check for subsection %s, user %s",
                 content_id, user_id)
        user = User.objects.get(id=user_id)
        store = modulestore()
        course_key = content_id.course_key

        # Get fresh list of all subsections in course order
        all_subsections = _get_all_subsections_in_course(course_key)

        if not all_subsections:
            log.warning("[UNSKIPPABLE-GATING] No subsections found in course")
            return True, {'url': None, 'display_name': None}

        current_position = None

        # Find position of current subsection
        for idx, (subsection_key, section_idx, subsection_idx) in enumerate(all_subsections):
            if str(subsection_key) == str(content_id):
                current_position = idx
                log.info("[UNSKIPPABLE-GATING] Found current subsection at position %d [%d.%d]",
                        idx, section_idx, subsection_idx)
                break

        # Handle case where current subsection was deleted/not found
        if current_position is None:
            log.warning("[UNSKIPPABLE-GATING] Current subsection %s not found in course (may have been deleted)",
                       content_id)
            return True, {'url': None, 'display_name': None}

        # Find the most recent unskippable checkpoint BEFORE the current subsection
        most_recent_checkpoint_key = None
        most_recent_checkpoint_position = None

        for idx in range(current_position - 1, -1, -1):  # Scan backwards from current position
            subsection_key, section_idx, subsection_idx = all_subsections[idx]
            try:
                if _subsection_has_unskippable_unit(subsection_key):
                    # Found a checkpoint before the current subsection
                    most_recent_checkpoint_key = subsection_key
                    most_recent_checkpoint_position = idx
                    log.info("[UNSKIPPABLE-GATING] Found most recent checkpoint at position %d [%d.%d]: %s",
                            idx, section_idx, subsection_idx, subsection_key)
                    break
            except Exception as e:
                log.error("[UNSKIPPABLE-GATING] Error checking subsection [%d.%d] %s: %s",
                         section_idx, subsection_idx, subsection_key, e)
                continue

        # If no checkpoint found before this subsection, it's unlocked
        if most_recent_checkpoint_key is None:
            log.info("[UNSKIPPABLE-GATING] ✓ No checkpoint before subsection %s - unlocked", content_id)
            return True, {'url': None, 'display_name': None}

        # Check if the checkpoint is completed
        try:
            completion = get_subsection_completion_percentage(most_recent_checkpoint_key, user)
            log.info("[UNSKIPPABLE-GATING] Checkpoint %s completion: %.1f%%",
                     most_recent_checkpoint_key, completion)

            if completion >= 100.0:
                # Checkpoint is complete - subsection is unlocked
                log.info("[UNSKIPPABLE-GATING] ✓ Subsection %s is unlocked - checkpoint %s is complete",
                        content_id, most_recent_checkpoint_key)
                return True, {'url': None, 'display_name': None}
            else:
                # Checkpoint is incomplete - subsection is LOCKED
                checkpoint_subsection = store.get_item(most_recent_checkpoint_key)
                prereq_meta_info = {
                    'url': reverse('jump_to', kwargs={'course_id': course_key, 'location': most_recent_checkpoint_key}),
                    'display_name': getattr(checkpoint_subsection, 'display_name', 'Required Content'),
                    'id': str(most_recent_checkpoint_key)
                }
                log.info(
                    "[UNSKIPPABLE-GATING] ✗ Subsection %s LOCKED - must complete checkpoint %s (%.1f%% complete)",
                    content_id, most_recent_checkpoint_key, completion
                )
                return False, prereq_meta_info
        except Exception as e:
            log.error("[UNSKIPPABLE-GATING] Error checking checkpoint %s completion: %s",
                     most_recent_checkpoint_key, e, exc_info=True)
            # If we can't check completion, don't block access
            return True, {'url': None, 'display_name': None}

    except Exception as e:
        log.error("[UNSKIPPABLE-GATING] Error checking unskippable_unit prerequisites for %s, user %s: %s",
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

    New logic:
    - First check for manually set stored milestones (enable_subsection_gating = True)
    - If no stored milestones, check for unskippable_unit checkpoints

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

    if not course:
        log.warning("[PREREQ-MILESTONE] Could not retrieve course for %s", course_key)
        return None

    enable_subsection_gating = getattr(course, 'enable_subsection_gating', True)

    log.info("[PREREQ-MILESTONE] Course %s - enable_subsection_gating=%s, relationship=%s",
             course_key, enable_subsection_gating, relationship)

    # If enable_subsection_gating is True, use stored milestones
    if enable_subsection_gating:
        log.info("[PREREQ-MILESTONE] Using stored milestones (manual gating)")
        try:
            return find_gating_milestones(course_key, content_key, relationship)[0]
        except IndexError:
            # No stored milestone found, fall through to check unskippable_unit
            log.info("[PREREQ-MILESTONE] No stored milestone found, checking unskippable_unit")

    # Check for unskippable_unit prerequisites
    if relationship == 'requires':
        has_unskippable_prereq = _has_unskippable_prerequisite_requirement(content_key)
        log.info("[PREREQ-MILESTONE] Unskippable_unit prerequisite requirement for %s: %s",
                 content_key, has_unskippable_prereq)
        if has_unskippable_prereq:
            # Return a pseudo-milestone to indicate unskippable_unit prerequisite exists
            log.info("[PREREQ-MILESTONE] Returning pseudo-milestone for unskippable_unit gating")
            return {
                'id': 'unskippable-unit-prereq',
                'namespace': 'unskippable-unit-gating',
                'unskippable_unit': True  # Special flag to indicate this is unskippable_unit gating
            }

    # Otherwise return None (no prerequisite)
    log.info("[PREREQ-MILESTONE] No prerequisites found, returning None")
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

    New logic:
    - If enable_subsection_gating = True and manual milestones exist, check those
    - Otherwise, a gate is fulfilled when the subsection is 100% complete (for unskippable_unit)

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

    enable_subsection_gating = getattr(course, 'enable_subsection_gating', True)

    # If enable_subsection_gating = True, check for stored milestones first
    if enable_subsection_gating:
        gating_milestone = get_gating_milestone(course_key, gating_content_key, "fulfills")
        if gating_milestone and 'unskippable_unit' not in gating_milestone.get('namespace', ''):
            # This is a manually set milestone, check it
            unfulfilled_milestones = [
                m['content_id'] for m in find_gating_milestones(
                    course_key,
                    None,
                    'requires',
                    {'id': user_id}
                ) if m['namespace'] == gating_milestone['namespace']
            ]
            return not unfulfilled_milestones

    # For unskippable_unit, a gate is fulfilled when the subsection is 100% complete
    user = User.objects.get(id=user_id)
    completion = get_subsection_completion_percentage(gating_content_key, user)

    log.info("[PREREQ] is_gate_fulfilled for subsection %s, user %s: completion=%.1f%%",
             gating_content_key, user_id, completion)

    return completion >= 100.0


def compute_is_prereq_met(content_id, user_id, recalc_on_unmet=False):
    """
    Returns true if the prerequisite has been met for a given milestone.
    Will recalculate the subsection grade if specified and prereq unmet

    New prerequisite logic:
    - Check for manually set stored milestones (enable_subsection_gating = True)
    - If no stored milestones, check unskippable_unit checkpoints

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
    enable_subsection_gating = getattr(course, 'enable_subsection_gating', True)

    log.info("[PREREQ] Course %s settings - enable_subsection_gating=%s",
             course_key, enable_subsection_gating)

    # Check for unskippable_unit checkpoints (always active now)
    return _check_unskippable_unit_prerequisites(content_id, user_id)


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
                # Check if this subsection is an unskippable checkpoint
                # If so, it should NOT be auto-completed even if empty
                try:
                    if _subsection_has_unskippable_unit(subsection_usage_key):
                        log.info(
                            "[UNSKIPPABLE-GATING] Subsection %s is an unskippable checkpoint with no "
                            "completable blocks - returning 0%% completion (not auto-completed)",
                            subsection_usage_key
                        )
                        return 0.0
                except Exception as e:
                    log.warning(
                        "Error checking if subsection %s is unskippable: %s - treating as regular subsection",
                        subsection_usage_key, e
                    )

                # For regular subsections with no completable blocks, return 100%
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
