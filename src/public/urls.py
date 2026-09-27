from django.urls import path

from public import views

app_name = "public"

urlpatterns = [
    path("", views.home, name="home"),
    path("events/", views.event_list, name="event_list"),
    path("events/<slug:slug>", views.event_detail, name="event_detail"),
    # No trailing slash on purpose: /projects is the exact path .dogfood.toml advertises, and a
    # slash-redirect in front of it would make the acceptance checker measure a 301.
    path("projects", views.gallery, name="gallery"),
    path("projects/<int:project_id>", views.project_detail, name="project"),
]
