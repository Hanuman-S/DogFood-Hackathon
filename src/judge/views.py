"""The judge portal. Assignments and scoring arrive here with T2."""

from django.shortcuts import render

from accounts.guards import portal_required


@portal_required("judge")
def home(request):
    return render(
        request,
        "judge/home.html",
        {
            "modules": [
                ("assignments", "the projects you have been asked to review"),
                ("scoring", "score each project against the weighted rubric"),
                ("my scores", "your own scores, never anyone else's"),
            ]
        },
    )
