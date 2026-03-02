"""
Learner Home URL routing configuration
"""

from django.urls import path
from django.urls import include, re_path

from lms.djangoapps.learner_home import views
from lms.djangoapps.learner_home import networked_views

app_name = "learner_home"

# Learner Dashboard Routing
urlpatterns = [
    re_path(r"^init/?", views.InitializeView.as_view(), name="initialize"),
    path("mock/", include("lms.djangoapps.learner_home.mock.urls")),
    path("networked/courses/", networked_views.NetworkedCourseListView.as_view(), name="networked_course_list"),
    path("networked/course-detail/<path:course_id>/", networked_views.NetworkedCourseDetailView.as_view(), name="networked_course_detail")
]
