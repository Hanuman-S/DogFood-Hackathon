"""Event URLs, mounted at `/events/` by `config/urls.py`.

`/events/<slug>/projects` (the per-event gallery) is deliberately **not** here -- it belongs to the
gallery app, which owns every public project listing so that the visibility rules live in one place.
"""

from django.urls import path

from events import views

urlpatterns = [
    path("", views.event_list, name="event_list"),
    path("new", views.event_create, name="event_create"),
    path("<slug:slug>", views.event_detail, name="event_detail"),
    path("<slug:slug>/register", views.register, name="event_register"),
    path("<slug:slug>/edit", views.event_edit, name="event_edit"),
    path("<slug:slug>/manage", views.dashboard, name="event_dashboard"),
    # tracks
    path("<slug:slug>/tracks/new", views.track_form, name="track_create"),
    path("<slug:slug>/tracks/<int:track_id>", views.track_form, name="track_edit"),
    path("<slug:slug>/tracks/<int:track_id>/delete", views.track_delete, name="track_delete"),
    # prizes
    path("<slug:slug>/prizes/new", views.prize_form, name="prize_create"),
    path("<slug:slug>/prizes/<int:prize_id>", views.prize_form, name="prize_edit"),
    path("<slug:slug>/prizes/<int:prize_id>/delete", views.prize_delete, name="prize_delete"),
    # custom questions
    path("<slug:slug>/questions/new", views.question_form, name="question_create"),
    path("<slug:slug>/questions/<int:question_id>", views.question_form, name="question_edit"),
    path(
        "<slug:slug>/questions/<int:question_id>/delete",
        views.question_delete,
        name="question_delete",
    ),
    # memberships
    path("<slug:slug>/members/add", views.membership_add, name="membership_add"),
    path(
        "<slug:slug>/members/<int:membership_id>/remove",
        views.membership_remove,
        name="membership_remove",
    ),
]
