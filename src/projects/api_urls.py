from django.urls import path

from projects import api

app_name = "projects_api"

urlpatterns = [
    path("events", api.events, name="events"),
    path("events/<slug:slug>", api.event_detail, name="event"),
    path("events/<slug:slug>/projects", api.project_create, name="project_create"),
    path("projects", api.projects, name="projects"),
    path("projects/<int:project_id>", api.project_detail, name="project"),
    path("projects/<int:project_id>/submit", api.project_submit, name="project_submit"),
    path("projects/<int:project_id>/unsubmit", api.project_unsubmit, name="project_unsubmit"),
]
