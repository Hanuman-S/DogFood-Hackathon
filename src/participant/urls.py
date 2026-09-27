from django.urls import path

from participant import views, voting

app_name = "participant"

urlpatterns = [
    path("", views.home, name="home"),
    path("events/<slug:slug>/", views.event, name="event"),
    path("events/<slug:slug>/team", views.team_create, name="team_create"),
    path("events/<slug:slug>/project", views.project_start, name="project_start"),
    path("events/<slug:slug>/vote", voting.vote, name="vote"),
    path("events/<slug:slug>/vote/open", voting.vote_open, name="vote_open"),
    path("events/<slug:slug>/vote/cast", voting.vote_cast, name="vote_cast"),
    path("teams/<int:team_id>/rename", views.team_rename, name="team_rename"),
    path("teams/<int:team_id>/reset-link", views.team_reset_link, name="team_reset_link"),
    path("teams/<int:team_id>/members/<int:member_id>/captain", views.team_captain, name="team_captain"),
    path("teams/<int:team_id>/members/<int:member_id>/remove", views.team_remove, name="team_remove"),
    path("teams/<int:team_id>/leave", views.team_leave, name="team_leave"),
    path("projects/<int:project_id>/", views.project_edit, name="project_edit"),
    path("projects/<int:project_id>/preview", views.project_preview, name="project_preview"),
    path("projects/<int:project_id>/submit", views.project_submit, name="project_submit"),
    path("projects/<int:project_id>/unsubmit", views.project_unsubmit, name="project_unsubmit"),
    path("projects/<int:project_id>/images", views.image_add, name="image_add"),
    path("projects/<int:project_id>/images/<int:image_id>/remove", views.image_remove, name="image_remove"),
]
