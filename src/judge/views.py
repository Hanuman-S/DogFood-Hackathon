"""The judge portal: assignments, progress and scoring.

Anyone who judges at least one event may enter; each page only ever shows the events this
account judges (roles are per event). The one page here open to anyone is the invite link
(`invite`): the token is what authorizes it, and it only works for the invited email.
"""

from django.contrib import messages
from django.http import Http404
from django.shortcuts import redirect, render
from django.urls import reverse
from django.views.decorators.cache import never_cache

from accounts import services as account_services
from accounts.forms import InviteAccountForm
from accounts.guards import portal_required
from accounts.models import User
from accounts.roles import Role
from core.judging import JudgingNotOpen, judging_window
from events import services as event_services
from events.models import Event, EventMembership
from scoring import services as scoring
from scoring.models import Criterion


@never_cache
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


@never_cache
def invite(request, token):
    """A judge invite link. Shows the event, then -- depending on who is looking --

    * signed in as the invited email: an "accept" button;
    * signed in as someone else: why it will not work for them;
    * signed out, and the email already has an account: a link to log in and come back;
    * signed out, no account yet: a form to create one (email fixed), which accepts in one step.
      This works even when public sign-up is closed: the organizer's invite is the permission.

    An **open** link (no email) takes anyone, once: a signed-in account can accept it directly,
    and a visitor either logs in or creates an account with their own email from the link.
    """
    invite = event_services.find_judge_invite(token)
    state = event_services.judge_invite_state(invite) if invite else "invalid"
    if state != "open":
        return render(request, "judge/invite.html", {"state": state, "invite": invite},
                      status=404 if invite is None else 410)

    user = request.user if request.user.is_authenticated else None
    open_link = not invite.email
    has_account = user is not None or (not open_link and User.objects.filter(email=invite.email).exists())
    problem = ""
    if user is not None:
        if not open_link and user.email != invite.email:
            problem = (f"this invite is for {invite.email}, and you are logged in as {user.email}. "
                       "log out, then open the link again.")
        else:
            problem = event_services.judge_invite_problem(invite.event, user)
    form = None if has_account else InviteAccountForm(request.POST or None, email=invite.email or None)
    status = 200

    if request.method == "POST":
        if user is None and form is not None:
            if form.is_valid():
                user = account_services.register_participant(
                    request, name=form.cleaned_data["name"],
                    email=invite.email or form.cleaned_data["email"],
                    password=form.cleaned_data["password1"],
                )
            else:
                status = 400
        if user is not None and status == 200:
            try:
                membership = event_services.accept_judge_invite(request, token, user)
            except event_services.EventRuleError as error:
                messages.error(request, str(error))
                return redirect(request.path)
            messages.success(request, f"you are now a judge of {membership.event.name}.")
            return redirect("judge:home")

    return render(request, "judge/invite.html", {
        "state": state, "invite": invite, "event": invite.event, "tracks": list(invite.tracks.all()),
        "user_here": user, "has_account": has_account, "problem": problem, "form": form,
        "open_link": open_link,
        "login_url": f"{reverse('accounts:login')}?next={request.path}",
    }, status=status)


def _judged_event(request, slug):
    """The event and the caller's judge membership in it. 404 for anyone who does not judge it,
    so the page does not even confirm the event exists to them."""
    event = Event.objects.filter(slug=slug).first()
    membership = scoring.judge_membership(request.user, event) if event else None
    if membership is None:
        raise Http404("No such event.")
    return event, membership


@never_cache
@portal_required("judge")
def event_detail(request, slug):
    """The judge console: the queue, what is drafted and submitted, the rubric, the window."""
    event, membership = _judged_event(request, slug)
    progress = scoring.judge_progress(membership)
    next_row = next((r for r in progress["projects"] if r["status"] != "submitted"), None)
    context = {
        "event": event, "membership": membership, "progress": progress,
        "next_project_id": next_row["project"].pk if next_row else None,
        "window": judging_window(event),
    }
    if request.GET.get("partial") == "1":  # the queue alone, for the console's live refresh
        return render(request, "judge/_queue.html", context)
    return render(request, "judge/event.html", context)


@never_cache
@portal_required("judge")
def project_score(request, slug, project_id):
    """One review: read the project, score each criterion, save a draft or submit; or declare a
    conflict of interest. Only projects in the judge's queue are reachable (404 otherwise)."""
    event, membership = _judged_event(request, slug)
    queue = list(scoring.judge_queue(membership))
    positions = {a.project_id: i for i, a in enumerate(queue)}
    if project_id not in positions:
        raise Http404("This project is not in your queue.")
    assignment = queue[positions[project_id]]
    project = assignment.project
    following = queue[positions[project_id] + 1].project if positions[project_id] + 1 < len(queue) else None
    criteria = scoring.with_shares(Criterion.objects.filter(event=event).order_by("order", "key"))

    posted = None  # a refused review is shown again as the judge typed it, not as last saved
    if request.method == "POST":
        try:
            if request.POST.get("action") == "decline":
                scoring.decline_assignment(request, assignment, request.POST.get("reason", ""))
                messages.success(request, f"you declined {project.name}; the organizers will reassign it.")
                return redirect("judge:event", slug=event.slug)
            submit = request.POST.get("action") in ("submit", "submit_next")
            values = {c.key: request.POST.get(f"criterion_{c.key}", "") for c in criteria}
            scoring.save_review(request, membership, project, values, request.POST.get("comment", ""),
                                submit=submit)
            messages.success(request, f"review of {project.name} {'submitted' if submit else 'saved as a draft'}.")
            if request.POST.get("action") == "submit_next" and following:
                return redirect("judge:project_score", slug=event.slug, project_id=following.pk)
            return redirect("judge:event", slug=event.slug)
        except (JudgingNotOpen, scoring.ReviewError, scoring.AssignmentError) as refusal:
            messages.error(request, str(refusal))
            if request.POST.get("action") != "decline":
                posted = {c.pk: request.POST.get(f"criterion_{c.key}", "") for c in criteria}

    score = membership.scores.filter(project=project).prefetch_related("items").first()
    given = {i.criterion_id: i.value for i in score.items.all()} if score else {}
    if posted is not None:
        given = {pk: int(v) for pk, v in posted.items() if v.strip().isdigit()}
    criteria_data = []
    for c in criteria:
        value = int(given[c.pk]) if c.pk in given and given[c.pk] == int(given[c.pk]) else given.get(c.pk)
        levels = [(str(v), c.level_descriptions.get(str(v), "")) for v in range(c.min_value, c.max_value + 1)]
        criteria_data.append({
            "criterion": c,
            "value": value,
            "range": range(c.min_value, c.max_value + 1),
            "levels": levels,
            # (value, its description) per rating button; the page shows only the chosen one's text
            "options": [(int(v), text) for v, text in levels],
            "has_levels": any(text for _, text in levels),
            "chosen_text": c.level_descriptions.get(str(value), "") if value is not None else "",
        })
    return render(request, "judge/project_score.html", {
        "event": event, "membership": membership, "project": project, "criteria_data": criteria_data,
        "score": score, "submitted": bool(score and score.submitted_at), "comment": request.POST.get("comment", "") if posted is not None else (score.comment if score else ""),
        "answers": project.shown_answers(), "next_project": following, "window": judging_window(event),
    })
