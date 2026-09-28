from django.urls import path

from projects import api, comments_api

app_name = "projects_api"

urlpatterns = [
    path("events", api.events, name="events"),
    path("events/<slug:slug>", api.event_detail, name="event"),
    path("events/<slug:slug>/projects", api.project_create, name="project_create"),
    path("projects", api.projects, name="projects"),
    path("projects/<int:project_id>", api.project_detail, name="project"),
    path("projects/<int:project_id>/submit", api.project_submit, name="project_submit"),
    path("projects/<int:project_id>/unsubmit", api.project_unsubmit, name="project_unsubmit"),
    path("projects/<int:project_id>/comments", comments_api.project_comments, name="project_comments"),
    path("comments/<int:comment_id>/delete", comments_api.comment_delete, name="comment_delete"),
    path("comments/<int:comment_id>/hide", comments_api.comment_hide, name="comment_hide"),
    path("comments/<int:comment_id>/restore", comments_api.comment_restore, name="comment_restore"),
]
