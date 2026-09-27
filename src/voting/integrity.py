"""Evidence for the organizer's "voting integrity" page. Read-only: computed from the ballots and the
audit log each time the page is shown (a GET never writes), and nothing is ever removed
automatically -- a flag is a reason to look, and the organizer decides whether to void.

Flags consider only ballots that place credits and are not voided. An opened-but-empty ballot is
counted separately: every empty ballot is identical to every other, so counting them would bury the
real signal.

* ip_burst: VOTE_FLAG_IP_BALLOTS or more ballots created from one IP hash within VOTE_FLAG_WINDOW.
  (A venue's shared network can trip this honestly; it is a prompt to look, not a verdict.)
* identical_ballots: two or more ballots with exactly the same credits on the same projects, last
  changed within VOTE_FLAG_WINDOW of each other. Only ballots spread over two or more projects: "all
  on one project" is the most common honest ballot, so on its own it says nothing.
* new_account: a logged-in voter whose account was created within VOTE_FLAG_NEW_ACCOUNT before their
  ballot was.

Position bias: the average credits placed at each shown position (over ballots that place credits,
voided ones left out). With the per-ballot shuffle, a flat profile is the evidence that no project
gained from being shown first.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass, field

from django.conf import settings
from django.db.models import Avg, Count, Exists, OuterRef

from core.models import AuditAction, AuditLog
from projects.models import Project

from .models import Ballot, BallotLine
from .services import ballot_label

AUDIT_ACTIONS = (
    AuditAction.BALLOT_OPENED, AuditAction.VOTE_CAST, AuditAction.VOTE_CHANGED, AuditAction.VOTE_REFUSED,
    AuditAction.VOTE_LATE_REFUSED, AuditAction.VOTE_THROTTLED, AuditAction.BALLOT_VOIDED,
    AuditAction.BALLOT_VOID_REFUSED, AuditAction.BALLOT_RESTORED, AuditAction.BALLOT_RESTORE_REFUSED,
    AuditAction.VOTING_CONFIG_CHANGED, AuditAction.VOTING_CONFIG_REFUSED,
    AuditAction.VOTING_ENDED_EARLY, AuditAction.VOTING_END_REFUSED, AuditAction.VOTING_REMOVED,
    AuditAction.VOTER_LINKS_ADDED, AuditAction.VOTER_LINK_REVOKED, AuditAction.VOTER_LINKS_EXPORTED,
    AuditAction.OPEN_LINK_ROTATED, AuditAction.TALLY_EXPORTED, AuditAction.VOTING_BYPASSED,
)


@dataclass
class Flag:
    kind: str
    title: str
    detail: str
    ballots: list = field(default_factory=list)


def _ballots(event):
    placed = BallotLine.objects.filter(ballot=OuterRef("pk"), credits__gt=0)
    return (Ballot.objects.filter(event=event).annotate(placed=Exists(placed))
            .select_related("voter_user", "voter_link", "voided_by").prefetch_related("lines"))


def counts(event):
    rows = list(_ballots(event).values_list("placed", "voided_at"))
    return {
        "opened": len(rows),
        "with_votes": sum(1 for placed, voided in rows if placed and voided is None),
        "empty": sum(1 for placed, voided in rows if not placed and voided is None),
        "voided": sum(1 for _, voided in rows if voided is not None),
    }


def _clusters(items, when, window):
    """Groups of consecutive items (sorted by `when`) each within `window` of the one before."""
    items = sorted(items, key=when)
    groups, current = [], []
    for item in items:
        if current and when(item) - when(current[-1]) > window:
            groups.append(current)
            current = []
        current.append(item)
    if current:
        groups.append(current)
    return groups


def _signature(ballot):
    return tuple(sorted((line.project_id, line.credits) for line in ballot.lines.all() if line.credits))


def flags(event):
    window = settings.VOTE_FLAG_WINDOW
    live = [b for b in _ballots(event).filter(voided_at__isnull=True) if b.placed]
    found = []

    by_ip = defaultdict(list)
    for b in live:
        if b.created_ip_hash:
            by_ip[b.created_ip_hash].append(b)
    for ip, group in by_ip.items():
        for cluster in _clusters(group, lambda b: b.created_at, window):
            # a burst is N ballots inside one window, not a slow chain of them
            burst = [b for b in cluster if sum(1 for o in cluster if abs((o.created_at - b.created_at)) <= window)
                     >= settings.VOTE_FLAG_IP_BALLOTS]
            if len(burst) >= settings.VOTE_FLAG_IP_BALLOTS:
                found.append(Flag("ip_burst", f"{len(burst)} ballots from one network",
                                  f"network {ip[:8]}, created within {int(window.total_seconds() // 60)} minutes", burst))

    by_signature = defaultdict(list)
    for b in live:
        signature = _signature(b)
        if len(signature) >= 2:
            by_signature[signature].append(b)
    for signature, group in by_signature.items():
        for cluster in _clusters(group, lambda b: b.updated_at, window):
            if len(cluster) >= 2:
                spread = ", ".join(f"{c} on #{pk}" for pk, c in signature)
                found.append(Flag("identical_ballots", f"{len(cluster)} identical ballots",
                                  f"{spread}, within {int(window.total_seconds() // 60)} minutes of each other", cluster))

    fresh = settings.VOTE_FLAG_NEW_ACCOUNT
    for b in live:
        if b.voter_user_id and b.created_at - b.voter_user.date_joined <= fresh:
            minutes = max(0, int((b.created_at - b.voter_user.date_joined).total_seconds() // 60))
            found.append(Flag("new_account", "new account voted at once",
                              f"account created {minutes} minute{'s' if minutes != 1 else ''} before voting", [b]))
    return found


def voided(event):
    return list(_ballots(event).filter(voided_at__isnull=False).order_by("-voided_at"))


def position_bias(event):
    """[(position shown, 1-based; average credits; lines)] over ballots that place credits, voided left out."""
    placed = BallotLine.objects.filter(ballot=OuterRef("ballot"), credits__gt=0)
    rows = (BallotLine.objects.filter(ballot__event=event, ballot__voided_at__isnull=True)
            .annotate(placed=Exists(placed)).filter(placed=True)
            .values("shown_position").annotate(avg=Avg("credits"), n=Count("id")).order_by("shown_position"))
    return [(r["shown_position"] + 1, float(r["avg"]), r["n"]) for r in rows]


@dataclass
class TrailRow:
    at: object
    who: str
    action: str
    code: str
    summary: str


def _names(event):
    return {str(pk): name for pk, name in Project.objects.filter(event=event).values_list("pk", "name")}


def _credits_text(credits, names):
    if not credits:
        return "nothing"
    return ", ".join(f"{names.get(str(pk), '#' + str(pk))} {c}" for pk, c in credits.items())


def trail(event, limit=200):
    """The voting audit trail, newest first, in words: who, what, and the details that matter."""
    names = _names(event)
    rows = []
    for e in AuditLog.objects.filter(subject=event.slug, action__in=AUDIT_ACTIONS).order_by("-created_at", "-id")[:limit]:
        d = e.detail or {}
        who = d.get("voter") or e.actor_email or "anonymous"
        if e.action in (AuditAction.VOTE_CAST, AuditAction.VOTE_CHANGED):
            summary = f"ballot #{d.get('ballot')}: {_credits_text(d.get('before'), names)} -> {_credits_text(d.get('after'), names)}"
            if e.action == AuditAction.VOTE_CAST:
                summary = f"ballot #{d.get('ballot')}: {_credits_text(d.get('after'), names)}"
        elif e.action in (AuditAction.VOTE_REFUSED, AuditAction.VOTE_LATE_REFUSED, AuditAction.VOTE_THROTTLED):
            summary = f"{d.get('attempted', '')}: {d.get('reason', '')}" + (f" ({d['limit']} limit)" if d.get("limit") else "")
        elif e.action == AuditAction.BALLOT_VOIDED:
            who = e.actor_email
            summary = f"ballot #{d.get('ballot')} of {d.get('voter')}: {d.get('reason')} (had {_credits_text(d.get('credits'), names)})"
        elif e.action == AuditAction.BALLOT_RESTORED:
            who = e.actor_email
            was = (d.get("undid") or {}).get("void_reason")
            summary = f"ballot #{d.get('ballot')} of {d.get('voter')}: {d.get('reason')} (voided for: {was})"
        elif e.action == AuditAction.BALLOT_OPENED:
            summary = f"ballot #{d.get('ballot')}"
        else:
            summary = d.get("reason") or ", ".join(f"{k}={v}" for k, v in d.items() if k not in ("before", "after"))[:200]
        rows.append(TrailRow(e.created_at, who, e.get_action_display(), e.action, summary))
    return rows


def ballot_rows(ballots):
    """(ballot, who, credits text) for the page."""
    return [(b, ballot_label(b), ", ".join(f"#{line.project_id}: {line.credits}" for line in b.lines.all() if line.credits))
            for b in ballots]
