from django.urls import path, register_converter

from organizer import assignments, progress, rubric, views


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
    path("events/<slug:slug>/organizers/invite", views.organizer_invite_create, name="organizer_invite_create"),
    path("events/<slug:slug>/rubric", rubric.rubric, name="rubric"),
    path("events/<slug:slug>/rubric/standard", rubric.rubric_standard, name="rubric_standard"),
    path("events/<slug:slug>/rubric/<int:criterion_id>", rubric.criterion_text, name="criterion_text"),
    path("events/<slug:slug>/judges", views.judge_add, name="judge_add"),
    path("events/<slug:slug>/judges/invite", views.judge_invite_create, name="judge_invite_create"),
    path("events/<slug:slug>/judges/invites/<int:invite_id>/revoke", views.judge_invite_revoke, name="judge_invite_revoke"),
    path("events/<slug:slug>/judges/<int:membership_id>/remove", views.judge_remove, name="judge_remove"),
    path("events/<slug:slug>/deadline/extend", views.deadline_extend, name="deadline_extend"),
    path("events/<slug:slug>/deadline/teams", views.extension_grant, name="extension_grant"),
    path("events/<slug:slug>/judging/extend", views.judging_extend, name="judging_extend"),
    path("events/<slug:slug>/assignments", assignments.assignments, name="assignments"),
    path("events/<slug:slug>/assignments/add", assignments.assignment_add, name="assignment_add"),
    path("events/<slug:slug>/assignments/<int:assignment_id>/withdraw", assignments.assignment_withdraw, name="assignment_withdraw"),
    path("events/<slug:slug>/assignments/<int:assignment_id>/move", assignments.assignment_move, name="assignment_move"),
    path("events/<slug:slug>/judges/<int:membership_id>/reassign", assignments.judge_reassign, name="judge_reassign"),
    path("events/<slug:slug>/progress", progress.progress, name="progress"),
    path("events/<slug:slug>/progress/nudge/<int:membership_id>", progress.nudge, name="judge_nudge"),
    path("events/<slug:slug>/deadline/teams/<int:extension_id>/revoke", views.extension_revoke, name="extension_revoke"),
    path("events/<slug:slug>/organizers/<int:link_id>/remove", views.organizer_remove, name="organizer_remove"),
]
