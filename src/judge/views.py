"""The judge portal. Assignments and scoring arrive here with T2.

Anyone who judges at least one event may enter; each page only ever shows the events this
account judges (roles are per event).
"""

from django.shortcuts import render

from accounts.guards import portal_required
from accounts.roles import Role
from events.models import EventMembership


@portal_required("judge")
def home(request):
    judging = (
        EventMembership.objects.filter(user=request.user, role=Role.JUDGE)
        .select_related("event")
        .prefetch_related("judge_tracks__track")
        .order_by("-event__submissions_close_at")
    )
    return render(
        request,
        "judge/home.html",
        {
            "judging": judging,
            "modules": [
                ("assignments", "the projects you have been asked to review"),
                ("scoring", "score each project against the weighted rubric"),
                ("my scores", "your own scores, never anyone else's"),
            ],
        },
    )
