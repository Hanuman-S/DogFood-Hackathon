from django.urls import path

from public import comments, views, voting

app_name = "public"

urlpatterns = [
    path("", views.home, name="home"),
    path("events/", views.event_list, name="event_list"),
    path("events/<slug:slug>", views.event_detail, name="event_detail"),
    path("events/<slug:slug>/results", views.event_results, name="event_results"),
    path("events/<slug:slug>/vote/<str:token>", voting.link_vote, name="link_vote"),
    path("events/<slug:slug>/vote/<str:token>/open", voting.link_vote_open, name="link_vote_open"),
    path("events/<slug:slug>/vote/<str:token>/cast", voting.link_vote_cast, name="link_vote_cast"),
    # No trailing slash on purpose: /projects is the exact path .dogfood.toml advertises, and a
    # slash-redirect in front of it would make the acceptance checker measure a 301.
    path("projects", views.gallery, name="gallery"),
    path("projects/<int:project_id>", views.project_detail, name="project"),
    path("projects/<int:project_id>/comments", comments.comment_post, name="comment_post"),
    path("comments/<int:comment_id>/delete", comments.comment_delete, name="comment_delete"),
]
