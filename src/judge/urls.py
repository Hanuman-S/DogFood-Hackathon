from django.urls import path

from judge import views

app_name = "judge"

urlpatterns = [
    path("", views.home, name="home"),
    path("events/<slug:slug>/", views.event_detail, name="event"),
    path("events/<slug:slug>/projects/<int:project_id>/", views.project_score, name="project_score"),
]
