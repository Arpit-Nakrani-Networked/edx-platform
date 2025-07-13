""" Views related to auth. """

from django.views.decorators.http import require_POST
from django.http import JsonResponse, HttpResponseBadRequest, HttpResponseForbidden
from django.contrib.auth.models import User
import json

# @ratelimit(key=POST_EMAIL_KEY, rate=settings.PASSWORD_RESET_EMAIL_RATE, block=False)
# @ratelimit(key=REAL_IP_KEY, rate=settings.PASSWORD_RESET_IP_RATE, block=False)
@require_POST
def user_role_change_handle(request, email):
    """
    Handle user role change requests (e.g., toggling 'is_staff' status).

    Args:
        request (HttpRequest)
        email (str): User's email address.

    Returns:
        JsonResponse: Response with status, message, and updated role.
    """
    if not email:
        return JsonResponse({'status': 400, 'message': 'Missing email parameter'}, status=400)

    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return JsonResponse({'status': 400, 'message': 'User not found'}, status=400)

    try:
        body = json.loads(request.body)
        is_staff = body.get('is_staff', True)  # Default to True if not provided
    except json.JSONDecodeError:
        is_staff = True  # Default to True if body is not valid JSON

    user.is_staff = bool(is_staff)
    user.save()

    return JsonResponse({
        'status': 200,
        'message': 'Course creator role updated successfully.',
        'email': email,
        'isCourseCreator': user.is_staff
    })