# common/djangoapps/custom_permission/middleware.py

import requests
from django.conf import settings
from django.http import JsonResponse

class ExternalAPIPermissionMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        # Skip static or admin routes if needed
        if request.path.startswith('/static/') or request.path.startswith('/admin/'):
            return self.get_response(request)

        # Check if user is authenticated (you may modify this logic)
        user = getattr(request, 'user', None)


        # if user and user.is_authenticated:
        #     try:
        #         # Call your external API to check permission
        #         response = requests.get(
        #             'https://other-server.com/api/check-permission/',
        #             headers={
        #                 'Authorization': f'Token {user.auth_token}'  # Or however you handle auth
        #             },
        #             timeout=3
        #         )
        #         if response.status_code != 200 or not response.json().get("allowed", False):
        #             return JsonResponse({'error': 'Permission denied'}, status=403)
        #     except requests.exceptions.RequestException:
        #         return JsonResponse({'error': 'Permission check failed'}, status=500)

        return self.get_response(request)
