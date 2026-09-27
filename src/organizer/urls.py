from django.urls import path, register_converter

from organizer import views


class PartKind:
    regex = "track|prize|question"

    def to_python(self, value):
        return value

    def to_url(self, value):
        return value


register_converter(PartKind, "part")

app_name = "organizer"

urlpatterns = [
    path("", views.home, name="home"),
    path("events/new", views.event_create, name="event_create"),
    path("events/<slug:slug>/", views.event_control, name="event"),
    path("events/<slug:slug>/publish", views.event_publish, name="event_publish"),
    path("events/<slug:slug>/<part:kind>/new", views.part_add, name="part_add"),
    path("events/<slug:slug>/<part:kind>/<int:part_id>", views.part_edit, name="part_edit"),
    path("events/<slug:slug>/<part:kind>/<int:part_id>/visibility", views.part_visibility, name="part_visibility"),
    path("events/<slug:slug>/<part:kind>/<int:part_id>/delete", views.part_delete, name="part_delete"),
    path("events/<slug:slug>/organizers", views.organizer_add, name="organizer_add"),
    path("events/<slug:slug>/judges", views.judge_add, name="judge_add"),
    path("events/<slug:slug>/judges/<int:membership_id>/remove", views.judge_remove, name="judge_remove"),
    path("events/<slug:slug>/deadline/extend", views.deadline_extend, name="deadline_extend"),
    path("events/<slug:slug>/deadline/teams", views.extension_grant, name="extension_grant"),
    path("events/<slug:slug>/deadline/teams/<int:extension_id>/revoke", views.extension_revoke, name="extension_revoke"),
    path("events/<slug:slug>/organizers/<int:link_id>/remove", views.organizer_remove, name="organizer_remove"),
]
