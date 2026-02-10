"""
Utility functions for Networked backend integration.
"""
import logging
import requests

log = logging.getLogger(__name__)


def get_admin_staff_emails_from_networked(request, log_prefix=""):
    """
    Retrieve admin/staff emails from the Networked backend API.

    This function checks for openedxCommunityId and openedxSessionToken cookies,
    makes an API call to the Networked backend to get admin/staff emails,
    and returns the list of emails.

    Args:
        request: Django request object containing cookies and host information
        log_prefix: Optional prefix for log messages (e.g., "[Gradebook]", "[Students]")

    Returns:
        list: A list of admin/staff email addresses, empty list if:
            - No openedxCommunityId cookie is present
            - API call fails
            - An error occurs during the process
    """
    # Check for cookies
    openedx_community_id = request.COOKIES.get('openedxCommunityId')
    openedx_session_token = request.COOKIES.get('openedxSessionToken')

    # Print cookies if they exist
    if openedx_community_id:
        print(f"{log_prefix} openedxCommunityId: {openedx_community_id}")
        log.info(f"{log_prefix} openedxCommunityId: {openedx_community_id}")
    if openedx_session_token:
        print(f"{log_prefix} openedxSessionToken: {openedx_session_token}")
        log.info(f"{log_prefix} openedxSessionToken: {openedx_session_token}")

    # Return empty list if no community ID is present
    if not openedx_community_id:
        return []

    admin_staff_emails = []

    try:
        # Determine backend URL based on request origin
        origin = request.get_host()

        if "lab" in origin:
            base_url = "https://backend.lab.networked.co"
        elif "qa" in origin or "local" in origin:
            base_url = "https://backend.qa.networked.co"
        else:
            base_url = "https://backend.networked.co"

        print(f"{log_prefix} Calling networked backend: {base_url}")
        log.info(f"{log_prefix} Calling networked backend: {base_url}")

        # Call networked backend API to get admin/staff emails
        api_url = f"{base_url}/api/v1/global/open-edx/{openedx_community_id}/admin-staff-emails"
        response = requests.get(api_url, timeout=10)

        print(f"{log_prefix} Networked backend response status: {response.status_code}")
        print(f"{log_prefix} Networked backend response headers: {dict(response.headers)}")
        print(f"{log_prefix} Networked backend response body: {response.text}")

        log.info(f"{log_prefix} Networked backend response status: {response.status_code}")

        if response.status_code == 200:
            data = response.json()
            print(f"{log_prefix} Networked backend response data: {data}")
            log.info(f"{log_prefix} Networked backend response data: {data}")

            admin_staff_emails = data.get('data', {}).get('emails', [])
            print(f"{log_prefix} Retrieved {len(admin_staff_emails)} admin/staff emails")
            log.info(f"{log_prefix} Retrieved {len(admin_staff_emails)} admin/staff emails")
        else:
            print(f"{log_prefix} Failed to get admin/staff emails: {response.status_code}")
            log.warning(f"{log_prefix} Failed to get admin/staff emails: {response.status_code}")

    except Exception as e:
        print(f"{log_prefix} Error fetching admin/staff emails: {e}")
        log.exception(f"{log_prefix} Error fetching admin/staff emails: {e}")

    return admin_staff_emails


def filter_admin_staff_from_list(items, admin_staff_emails, email_key='email', log_prefix=""):
    """
    Filter out admin/staff entries from a list based on email addresses.

    Args:
        items: List of dictionaries or objects to filter
        admin_staff_emails: List of admin/staff email addresses to exclude
        email_key: The key or attribute name to access the email (default: 'email')
        log_prefix: Optional prefix for log messages

    Returns:
        list: Filtered list with admin/staff entries removed
    """
    if not admin_staff_emails:
        return items

    original_count = len(items)

    # Handle both dict access and object attribute access
    if items and isinstance(items[0], dict):
        filtered_items = [item for item in items if item.get(email_key) not in admin_staff_emails]
    else:
        filtered_items = [item for item in items if getattr(item, email_key, None) not in admin_staff_emails]

    filtered_count = original_count - len(filtered_items)

    if filtered_count > 0:
        print(f"{log_prefix} Filtered out {filtered_count} admin/staff entries")
        log.info(f"{log_prefix} Filtered out {filtered_count} admin/staff entries")

    return filtered_items
