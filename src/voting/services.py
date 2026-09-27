"""Community voting rules. Every voting write goes through here; each writes its audit row.

Services take the actor, the voter and an `audit.Origin` / ip_hash, never the request: the views
extract those. Order of checks on every vote write:

  1. there is a vote in this event (404 no_voting);
  2. the window, by the database clock (409 voting_not_open / voting_closed, audited) -- first, so a
     late vote is refused as late, never as a 403 or a 400;
  3. who is voting (403 staff_cannot_vote: the event's judges and organizers and platform admins;
     403 account_too_new when the event refuses accounts created once voting opened);
  4. the lines: your own team's project (403 own_project), then shape (400 invalid_ballot);
  5. the budget, with the ballot row locked (400 over_budget).

A cast replaces the whole ballot: projects not named get 0 credits. So two tabs can never add up
to more than the budget; the row lock makes the second write wait for the first and then replace it.

The Postgres trigger (voting/migrations/0002) is the backstop for step 2.
"""

from __future__ import annotations

import hashlib
import hmac
import math
import random
import secrets
from dataclasses import dataclass

from django.core.exceptions import PermissionDenied
from django.db import DatabaseError, IntegrityError, transaction
from django.db.models import Max

from accounts.roles import STAFF_ROLES, is_admin, is_organizer_of, roles_in
from core import audit
from core.deadlines import db_now
from core.models import AuditAction
from events.models import Event
from projects.models import Project, Status
from scoring.models import Publication
from teams.models import TeamExtension, TeamMember

from . import links
from .errors import (AccountTooNew, InvalidAllowlist, InvalidBallot, InvalidVotingConfig,
                     LinkRevoked, NoSuchLink, NoVoting, OverBudget, OwnProject, StaffCannotVote, VotingClosed,
                     VotingConfigLocked, VotingNotOpen, WrongAccessMode)
from .models import CREDIT_BUDGET_MAX, AccessMode, Ballot, BallotLine, Method, VoterLink, VotingConfig

TRIGGER_MARKER = "dogfood_voting_closed"


@dataclass(frozen=True)
class Voter:
    """Who is voting, as the views identified them:

    * kind "user": a logged-in account (authenticated mode);
    * kind "link": an email link (email_gated); `user` is the account with that email, if any;
    * kind "cookie": the random id in an open-link cookie (open_link); `user` is the logged-in
      visitor, if any.

    `user` is who the account rules apply to (the event's judges and organizers, admins, accounts
    too new, your own team's project); None when the voter has no account we can match.
    """

    user: object = None
    kind: str = "user"
    link: object = None
    cookie: str = ""

    @classmethod
    def by_link(cls, link):
        from accounts.models import User

        return cls(user=User.objects.filter(email__iexact=link.email).first(), kind="link", link=link)

    @classmethod
    def by_cookie(cls, voter_id, user=None):
        return cls(user=user if getattr(user, "is_authenticated", False) else None, kind="cookie", cookie=voter_id)

    def ballot_filter(self):
        if self.kind == "link":
            return {"voter_link": self.link}
        if self.kind == "cookie":
            return {"voter_cookie": self.cookie}
        return {"voter_user": self.user}

    @property
    def label(self):
        if self.kind == "link":
            return f"link:{self.link.email}"
        if self.kind == "cookie":
            return f"open link:{self.cookie[:8]}"
        return self.user.email


MODE_OF_KIND = {"user": AccessMode.AUTHENTICATED, "link": AccessMode.EMAIL_GATED, "cookie": AccessMode.OPEN_LINK}


def voting_for(event):
    return VotingConfig.objects.filter(event=event).first()


def effective_close(event):
    """The last instant any team may still change its submission: the event's close or the latest
    team extension. Voting opens at or after it, so the ballot's project set is fixed."""
    latest = TeamExtension.objects.filter(team__event=event).aggregate(m=Max("until"))["m"]
    return max(event.submissions_close_at, latest) if latest else event.submissions_close_at


def state(config, now):
    if config is None:
        return "none"
    if now < config.opens_at:
        return "scheduled"
    if now < config.closes_at:
        return "open"
    return "closed"


def is_open(config, now):
    return state(config, now) == "open"


def close_blocked_by_voting(event, new_close):
    """A sentence if moving the submission close (or a team's extension) to `new_close` would let a
    project change after voting opens; "" otherwise. events.services asks this before any
    extension or close change (409 voting_scheduled)."""
    config = voting_for(event)
    if config is not None and new_close > config.opens_at:
        return (f"Community voting opens at {config.opens_at.isoformat()}, and the projects on the ballot "
                "must be final by then. Move voting later first.")
    return ""


# --- configuration ---------------------------------------------------------------------------------

LOCKED_ONCE_OPEN = ("opens_at", "access_mode", "method", "credit_budget", "accounts_before_open_only")


def _config_values(config):
    return {
        "opens_at": config.opens_at.isoformat(), "closes_at": config.closes_at.isoformat(),
        "access_mode": config.access_mode, "method": config.method, "credit_budget": config.credit_budget,
        "accounts_before_open_only": config.accounts_before_open_only,
    }


def set_voting_config(event, *, actor, opens_at, closes_at, access_mode, method, credit_budget=16,
                      accounts_before_open_only=True, origin=None) -> VotingConfig:
    """Create or change the event's vote. Refusals are audited (voting_config_refused):

    * PermissionDenied: not an organizer of the event (nor an admin).
    * InvalidVotingConfig (400): opens_at not before closes_at; opens_at before the effective
      submission close; a budget out of range; an unknown method or access mode.
    * VotingConfigLocked (409): once voting has opened, only closes_at may change, and it must stay
      in the future; once voting has closed nothing changes; closes_at may never be after an active
      publication of the results.
    """

    def refuse(error, reason):
        audit.record(AuditAction.VOTING_CONFIG_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     reason=reason)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can set up voting."), "not an organizer")
    if method not in Method.values:
        refuse(InvalidVotingConfig(f"method must be one of {', '.join(Method.values)}."), "invalid method")
    if access_mode not in AccessMode.values:
        refuse(InvalidVotingConfig(f"access_mode must be one of {', '.join(AccessMode.values)}."), "invalid access mode")
    if method == Method.ONE_PERSON_ONE_VOTE:
        credit_budget = 1
    if not isinstance(credit_budget, int) or isinstance(credit_budget, bool) \
            or not 1 <= credit_budget <= CREDIT_BUDGET_MAX:
        refuse(InvalidVotingConfig(f"credit_budget must be a whole number from 1 to {CREDIT_BUDGET_MAX}."),
               "invalid budget")
    if not opens_at < closes_at:
        refuse(InvalidVotingConfig("Voting must open before it closes."), "opens not before closes")

    wanted = {"opens_at": opens_at, "closes_at": closes_at, "access_mode": access_mode, "method": method,
              "credit_budget": credit_budget, "accounts_before_open_only": bool(accounts_before_open_only)}
    problem = None
    with transaction.atomic():
        Event.objects.select_for_update().filter(pk=event.pk).first()
        config = VotingConfig.objects.select_for_update().filter(event=event).first()
        now = db_now()
        before = _config_values(config) if config else None
        current = state(config, now)
        publication = Publication.objects.filter(event=event, unpublished_at__isnull=True).first()
        if current == "closed":
            problem = (VotingConfigLocked("Voting has closed; its settings are final."), "closed")
        elif current == "open":
            changed = [f for f in LOCKED_ONCE_OPEN if getattr(config, f) != wanted[f]]
            if changed:
                problem = (VotingConfigLocked(
                    f"Voting is open, so only the close can change (not {', '.join(changed)})."),
                    f"locked: {', '.join(changed)}")
            elif closes_at <= now:
                problem = (VotingConfigLocked("The new close must be in the future; use \"end voting now\" "
                                              "to close it at once."), "close in the past")
        else:
            close = effective_close(event)
            if opens_at < close:
                problem = (InvalidVotingConfig(
                    f"Voting can open at the submission close ({close.isoformat()}) at the earliest, "
                    "so the projects on the ballot are final."), "opens before submissions close")
        if problem is None and publication is not None and closes_at > publication.published_at:
            problem = (VotingConfigLocked("Results are published; voting cannot close after that."),
                       "after publication")
        if problem is None:
            if config is None:
                config = VotingConfig(event=event, ballot_secret=secrets.token_hex(32), created_by=actor)
            elif config.closes_at != closes_at and config.original_closes_at is None and current == "open":
                config.original_closes_at = config.closes_at
            for name, value in wanted.items():
                setattr(config, name, value)
            if config.access_mode == AccessMode.OPEN_LINK and not config.open_link_nonce:
                config.open_link_nonce = links.new_nonce()
            config.updated_by = actor
            config.save()
    if problem:
        refuse(*problem)
    after = _config_values(config)
    if before != after:
        audit.record(AuditAction.VOTING_CONFIG_CHANGED, origin=origin, actor=actor, subject=event.slug,
                     before=before, after=after)
    return config


def remove_voting(event, *, actor, origin=None):
    """Drop the event's vote, only before it opens (no ballot can exist yet)."""
    if not is_organizer_of(actor, event):
        raise PermissionDenied("Only the event's organizers can remove voting.")
    problem = None
    with transaction.atomic():
        config = VotingConfig.objects.select_for_update().filter(event=event).first()
        if config is None:
            problem = NoVoting("This event has no community vote.")
        elif state(config, db_now()) != "scheduled":
            problem = VotingConfigLocked("Voting has opened; it can be ended, not removed.")
        else:
            before = _config_values(config)
            config.delete()
    if problem:
        audit.record(AuditAction.VOTING_CONFIG_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     reason=f"remove: {problem.code}")
        raise problem
    audit.record(AuditAction.VOTING_REMOVED, origin=origin, actor=actor, subject=event.slug, before=before)


def end_voting_now(event, *, actor, origin=None) -> VotingConfig:
    """Close voting at this instant (closes_at = the database clock). Only ever earlier; refused
    (audited) once voting has closed, and before it opens. The config row is locked."""

    def refuse(error, reason):
        audit.record(AuditAction.VOTING_END_REFUSED, origin=origin, actor=actor, subject=event.slug, reason=reason)
        raise error

    if not is_organizer_of(actor, event):
        refuse(PermissionDenied("Only the event's organizers can end voting."), "not an organizer")
    problem = None
    with transaction.atomic():
        config = VotingConfig.objects.select_for_update().filter(event=event).first()
        now = db_now()
        if config is None:
            problem = (NoVoting("This event has no community vote."), "no voting")
        elif now >= config.closes_at:
            problem = (VotingConfigLocked(f"Voting already closed at {config.closes_at.isoformat()}."), "already ended")
        elif now <= config.opens_at:
            problem = (VotingNotOpen("Voting has not opened yet, so there is nothing to end."), "not started")
        else:
            old = config.closes_at
            if config.original_closes_at is None:
                config.original_closes_at = old
            config.closes_at = now
            config.updated_by = actor
            config.save(update_fields=["closes_at", "original_closes_at", "updated_by", "updated_at"])
    if problem:
        refuse(*problem)
    audit.record(AuditAction.VOTING_ENDED_EARLY, origin=origin, actor=actor, subject=event.slug,
                 old=old.isoformat(), new=now.isoformat())
    return config


# --- ballots --------------------------------------------------------------------------------------

def _refuse_vote(error, *, event, voter, origin, attempted, action=AuditAction.VOTE_REFUSED, **detail):
    audit.record(action, origin=origin, actor=voter.user, subject=event.slug, attempted=attempted,
                 reason=error.code, voter=voter.label, **detail)
    raise error


def _check_window(event, config, *, voter, origin, attempted):
    now = db_now()
    current = state(config, now)
    if current == "scheduled":
        _refuse_vote(VotingNotOpen(f"Voting opens at {config.opens_at.isoformat()}."), event=event, voter=voter,
                     origin=origin, attempted=attempted, action=AuditAction.VOTE_LATE_REFUSED,
                     opens_at=config.opens_at.isoformat())
    if current == "closed":
        _refuse_vote(VotingClosed(f"Voting closed at {config.closes_at.isoformat()}."), event=event, voter=voter,
                     origin=origin, attempted=attempted, action=AuditAction.VOTE_LATE_REFUSED,
                     closed_at=config.closes_at.isoformat())
    return now


def ineligibility(event, config, user):
    """Why `user` may not vote in `event`, as a VotingError -- or None. No account, no account rule."""
    if user is None:
        return None
    if is_admin(user):
        return StaffCannotVote("Platform admins can see every tally, so they cannot vote.")
    staff = roles_in(user, event) & STAFF_ROLES
    if staff:
        return StaffCannotVote(f"You are a {sorted(staff)[0]} of this event, so you cannot vote in it.")
    if config.accounts_before_open_only and user.date_joined >= config.opens_at:
        return AccountTooNew("This vote is open to accounts created before voting opened.")
    return None


def own_project_ids(event, user):
    if user is None:
        return set()
    teams = TeamMember.objects.filter(event=event, user=user).values("team_id")
    return set(Project.objects.filter(event=event, team_id__in=teams).values_list("pk", flat=True))


def ballot_project_ids(event, user):
    """The projects on `user`'s ballot: the event's submitted projects, less their own team's."""
    own = own_project_ids(event, user)
    return [pk for pk in Project.objects.filter(event=event, status=Status.SUBMITTED).order_by("pk")
            .values_list("pk", flat=True) if pk not in own]


def ballot_order(config, ballot_id, project_ids):
    """A stable shuffle for one ballot: seeded from the ballot id and the event's secret, so it
    differs between voters, never changes on reload, and cannot be predicted without the secret."""
    digest = hmac.new(config.ballot_secret.encode(), f"ballot:{ballot_id}".encode(), hashlib.sha256).digest()
    order = sorted(project_ids)
    random.Random(int.from_bytes(digest[:8], "big")).shuffle(order)
    return order


def ballot_of(event, voter):
    return Ballot.objects.filter(event=event, **voter.ballot_filter()).first()


def _create_ballot(event, config, voter, now, ip_hash):
    """The ballot with every project on it at 0 credits, in this ballot's own order. Inside the
    caller's transaction; a concurrent create for the same voter hits the unique constraint."""
    ballot = Ballot.objects.create(event=event, created_at=now, updated_at=now, ip_hash=ip_hash,
                                   **voter.ballot_filter())
    order = ballot_order(config, ballot.pk, ballot_project_ids(event, voter.user))
    BallotLine.objects.bulk_create(
        BallotLine(ballot=ballot, project_id=pk, credits=0, shown_position=i) for i, pk in enumerate(order)
    )
    return ballot


def _eligible(event, voter, *, origin, attempted):
    config = voting_for(event)
    if config is None:
        raise NoVoting("This event has no community vote.")
    _check_window(event, config, voter=voter, origin=origin, attempted=attempted)
    if MODE_OF_KIND[voter.kind] != config.access_mode:
        _refuse_vote(WrongAccessMode(f"This vote is by {config.get_access_mode_display().lower()}, not this way."),
                     event=event, voter=voter, origin=origin, attempted=attempted)
    if voter.kind == "link" and voter.link.revoked_at is not None:
        _refuse_vote(LinkRevoked("This voting link has been revoked."), event=event, voter=voter, origin=origin,
                     attempted=attempted)
    problem = ineligibility(event, config, voter.user)
    if problem:
        _refuse_vote(problem, event=event, voter=voter, origin=origin, attempted=attempted)
    return config


def _late_from_trigger(error, event, voter, origin, attempted):
    if isinstance(error, DatabaseError) and TRIGGER_MARKER in str(error):
        _refuse_vote(VotingClosed("Voting is closed."), event=event, voter=voter, origin=origin,
                     attempted=attempted, action=AuditAction.VOTE_LATE_REFUSED, by="database trigger")
    raise error


def open_ballot(event, voter, *, ip_hash="", origin=None) -> Ballot:
    """The voter's ballot, created (empty, in its own order) on their first explicit request. A
    POST, never a page load: a GET does not write."""
    config = _eligible(event, voter, origin=origin, attempted="open ballot")
    existing = ballot_of(event, voter)
    if existing is not None:
        return existing
    try:
        with transaction.atomic():
            ballot = _create_ballot(event, config, voter, db_now(), ip_hash)
    except IntegrityError:  # a second tab created it first
        return ballot_of(event, voter)
    except DatabaseError as error:
        _late_from_trigger(error, event, voter, origin, "open ballot")
    audit.record(AuditAction.BALLOT_OPENED, origin=origin, actor=voter.user, subject=event.slug, ballot=ballot.pk,
                 voter=voter.label)
    return ballot


def _parse_lines(lines, budget):
    if not isinstance(lines, dict):
        raise InvalidBallot('Send the ballot as {"<project id>": credits, ...}.')
    parsed = {}
    for key, value in lines.items():
        try:
            pk = int(str(key))
        except ValueError:
            raise InvalidBallot(f"{key!r} is not a project id.") from None
        if isinstance(value, bool) or not isinstance(value, (int, str)):
            raise InvalidBallot(f"Credits for project {pk} must be a whole number.")
        try:
            credits = int(value) if value != "" else 0
        except ValueError:
            raise InvalidBallot(f"Credits for project {pk} must be a whole number.") from None
        if credits < 0:
            raise InvalidBallot(f"Credits for project {pk} cannot be negative.")
        if credits > budget:
            raise OverBudget(f"{credits} credits on project {pk} is more than the whole budget of {budget}.")
        parsed[pk] = credits
    return parsed


def cast(event, voter, ip_hash, lines, actor=None, *, origin=None) -> Ballot:
    """Set `voter`'s whole ballot to `lines` ({project id: credits}; projects not named get 0).
    Creates the ballot on the first cast. See the module docstring for the order of refusals."""
    attempted = "cast"
    config = _eligible(event, voter, origin=origin, attempted=attempted)
    budget = config.budget
    raw = lines if isinstance(lines, dict) else {}
    own = own_project_ids(event, voter.user)
    for key, value in raw.items():
        if str(key).isdigit() and int(str(key)) in own and str(value) not in ("", "0"):
            _refuse_vote(OwnProject("You cannot vote for your own team's project."), event=event, voter=voter,
                         origin=origin, attempted=attempted, project=int(str(key)))
    try:
        wanted = _parse_lines(lines, budget)
    except (InvalidBallot, OverBudget) as error:
        _refuse_vote(error, event=event, voter=voter, origin=origin, attempted=attempted)
    wanted = {pk: c for pk, c in wanted.items() if not (pk in own and c == 0)}

    problem = None
    for attempt in (1, 2):  # a second tab creating the same ballot first -> retry once, on its row
        try:
            ballot, problem, before, after = _apply(event, config, voter, wanted, budget, ip_hash)
            break
        except IntegrityError:
            if attempt == 2:
                raise
        except DatabaseError as error:
            _late_from_trigger(error, event, voter, origin, attempted)
    if problem:
        _refuse_vote(problem, event=event, voter=voter, origin=origin, attempted=attempted, ballot=ballot.pk)
    first = not before
    audit.record(AuditAction.VOTE_CAST if first else AuditAction.VOTE_CHANGED, origin=origin, actor=voter.user,
                 subject=event.slug, ballot=ballot.pk, voter=voter.label,
                 before={str(k): v for k, v in sorted(before.items())},
                 after={str(k): v for k, v in sorted(after.items())}, total=sum(after.values()), budget=budget)
    return ballot


def _apply(event, config, voter, wanted, budget, ip_hash):
    """(ballot, problem or None, credits before, credits after), in one transaction with the
    ballot row and its lines locked."""
    problem, before, after = None, {}, {}
    with transaction.atomic():
        ballot = Ballot.objects.select_for_update().filter(event=event, **voter.ballot_filter()).first()
        now = db_now()
        if ballot is None:
            ballot = _create_ballot(event, config, voter, now, ip_hash)
            ballot = Ballot.objects.select_for_update().get(pk=ballot.pk)
        current = {line.project_id: line for line in ballot.lines.select_for_update()}
        unknown = sorted(set(wanted) - set(current))
        total = sum(wanted.values())
        if unknown:
            problem = InvalidBallot(f"Not on this ballot: project {', '.join(map(str, unknown))}.")
        elif total > budget:
            problem = OverBudget(f"This ballot places {total} credits; the budget is {budget}.")
        else:
            before = {pk: line.credits for pk, line in current.items() if line.credits}
            changed = []
            for pk, line in current.items():
                new = wanted.get(pk, 0)
                if line.credits != new:
                    line.credits = new
                    changed.append(line)
            BallotLine.objects.bulk_update(changed, ["credits"])
            ballot.updated_at, ballot.ip_hash = now, ip_hash
            ballot.save(update_fields=["updated_at", "ip_hash"])
            after = {pk: c for pk, c in wanted.items() if c}
    return ballot, problem, before, after


def ballot_view(event, voter):
    """(ballot or None, [(position, project, credits)]) for the voter's own page."""
    ballot = ballot_of(event, voter)
    if ballot is None:
        return None, []
    lines = ballot.lines.select_related("project", "project__team", "project__track").order_by("shown_position")
    return ballot, [(line.shown_position + 1, line.project, line.credits) for line in lines]


# --- the tally -------------------------------------------------------------------------------------

@dataclass
class TallyRow:
    project: Project
    influence: float
    ballots: int
    credits: int


def influence(credits):
    """One ballot's influence on one project: sqrt(credits). With a budget of 1 (one person, one
    vote) that is 0 or 1."""
    return math.sqrt(credits)


def tally(event, viewer):
    """Per project: total influence (sum of sqrt(credits) over non-voided ballots), the number of
    ballots that gave it anything, and the credits placed. Organizers of the event and platform
    admins only, at any time (PermissionDenied otherwise). Summed in ballot order, so the float
    total is the same on every call."""
    if not is_organizer_of(viewer, event):
        raise PermissionDenied("Only the event's organizers can see the vote tally.")
    rows = {p.pk: TallyRow(p, 0.0, 0, 0) for p in
            Project.objects.filter(event=event, status=Status.SUBMITTED).select_related("team", "track")}
    lines = (BallotLine.objects.filter(ballot__event=event, ballot__voided_at__isnull=True, credits__gt=0)
             .order_by("ballot_id", "project_id").values_list("project_id", "credits"))
    for project_id, credits in lines:
        row = rows.get(project_id)
        if row is not None:
            row.influence += influence(credits)
            row.ballots += 1
            row.credits += credits
    return sorted(rows.values(), key=lambda r: (-r.influence, r.project.name.lower(), r.project.pk))


def ballot_counts(event):
    ballots = Ballot.objects.filter(event=event)
    return {
        "ballots": ballots.count(),
        "voided": ballots.filter(voided_at__isnull=False).count(),
        "cast": ballots.filter(voided_at__isnull=True, lines__credits__gt=0).distinct().count(),
    }


def tally_export(event, *, actor, origin=None):
    """The tally for tally.csv, and the audit row for the download. Same gate as `tally`."""
    rows = tally(event, actor)
    config = voting_for(event)
    audit.record(AuditAction.TALLY_EXPORTED, origin=origin, actor=actor, subject=event.slug, rows=len(rows),
                 state=state(config, db_now()))
    return rows


# --- email links and the open link ----------------------------------------------------------------------

def _require_organizer(event, actor, what):
    if not is_organizer_of(actor, event):
        raise PermissionDenied(f"Only the event's organizers can {what}.")


def add_voter_links(event, *, actor, text="", csv_bytes=b"", origin=None):
    """Allowlist emails for email_gated voting: one link per email. Returns (created, reissued,
    unchanged, rejected entries). An email already allowlisted keeps its link; a revoked one gets a
    new link (new nonce, so the revoked link stays dead). Refused once voting has closed."""
    _require_organizer(event, actor, "allowlist voters")
    try:
        emails, rejected = links.parse_emails(text, csv_bytes)
    except ValueError as error:
        raise InvalidAllowlist(str(error)) from None
    if not emails:
        raise InvalidAllowlist("No valid email addresses found." + (f" Rejected: {', '.join(rejected[:5])}." if rejected else ""))
    config = voting_for(event)
    if config is not None and state(config, db_now()) == "closed":
        raise VotingConfigLocked("Voting has closed; no more voters can be added.")
    created, reissued, unchanged = [], [], []
    with transaction.atomic():
        now = db_now()
        existing = {link.email: link for link in VoterLink.objects.select_for_update().filter(event=event, email__in=emails)}
        for email in emails:
            link = existing.get(email)
            if link is None:
                link = VoterLink(event=event, email=email, nonce=links.new_nonce(), token_digest="pending:" + links.new_nonce(),
                                 created_at=now, created_by=actor)
                link.save()
                link.token_digest = links.digest(links.link_token(link))
                link.save(update_fields=["token_digest"])
                created.append(email)
            elif link.revoked_at is not None:
                link.nonce, link.revoked_at, link.revoked_by = links.new_nonce(), None, None
                link.token_digest = links.digest(links.link_token(link))
                link.save(update_fields=["nonce", "revoked_at", "revoked_by", "token_digest"])
                reissued.append(email)
            else:
                unchanged.append(email)
    audit.record(AuditAction.VOTER_LINKS_ADDED, origin=origin, actor=actor, subject=event.slug,
                 created=created, reissued=reissued, unchanged=len(unchanged), rejected=len(rejected))
    return created, reissued, unchanged, rejected


def revoke_voter_link(event, link_id, *, actor, origin=None):
    """Stop one link. Its ballot, if any, is kept as it is (void it separately to drop its votes)."""
    _require_organizer(event, actor, "revoke voter links")
    with transaction.atomic():
        link = VoterLink.objects.select_for_update().filter(event=event, pk=link_id).first()
        if link is None:
            raise NoSuchLink("No such voter link in this event.")
        if link.revoked_at is None:
            link.revoked_at, link.revoked_by = db_now(), actor
            link.save(update_fields=["revoked_at", "revoked_by"])
    audit.record(AuditAction.VOTER_LINK_REVOKED, origin=origin, actor=actor, subject=event.slug,
                 email=link.email, link=link.pk, had_ballot=link.ballots.exists())
    return link


VOTER_LINKS_HEADER = ["email", "link", "status", "ballot opened"]


def voter_links_rows(event, *, actor, base_url, origin=None):
    """voter-links.csv: one row per allowlisted email, with its link. Organizers only; audited."""
    _require_organizer(event, actor, "download voter links")
    opened = set(Ballot.objects.filter(event=event, voter_link__isnull=False).values_list("voter_link_id", flat=True))
    rows = [[link.email, f"{base_url}/events/{event.slug}/vote/{links.link_token(link)}",
             "revoked" if link.revoked_at else "active", link.pk in opened]
            for link in VoterLink.objects.filter(event=event).order_by("email")]
    audit.record(AuditAction.VOTER_LINKS_EXPORTED, origin=origin, actor=actor, subject=event.slug, rows=len(rows))
    return rows


def rotate_open_link(event, *, actor, origin=None):
    """A new open link; the old one stops working. Voters who already have a ballot keep it (their
    cookie still identifies them), but nobody new can arrive through the old link."""
    _require_organizer(event, actor, "change the open link")
    with transaction.atomic():
        config = VotingConfig.objects.select_for_update().filter(event=event).first()
        if config is None or config.access_mode != AccessMode.OPEN_LINK:
            raise WrongAccessMode("This event's vote does not use an open link.")
        if state(config, db_now()) == "closed":
            raise VotingConfigLocked("Voting has closed.")
        config.open_link_nonce = links.new_nonce()
        config.save(update_fields=["open_link_nonce", "updated_at"])
    audit.record(AuditAction.OPEN_LINK_ROTATED, origin=origin, actor=actor, subject=event.slug)
    return config


def resolve_token(event, token, *, origin=None):
    """("open", None) for the event's open link, ("link", VoterLink) for one of its email links.
    Anything else -- unknown, malformed, another event's -- is NoSuchLink (404), audited, the same
    answer for each so a token cannot be probed."""
    config = voting_for(event)
    if config is None:
        raise NoVoting("This event has no community vote.")
    token = (token or "")[:128]
    if links.is_open_token(config, token):
        return "open", None
    link = VoterLink.objects.filter(event=event, token_digest=links.digest(token)).first() if token else None
    if link is None:
        audit.record(AuditAction.VOTE_REFUSED, origin=origin, subject=event.slug, attempted="use a voting link",
                     reason="no_such_link", token_prefix=token[:6])
        raise NoSuchLink("This voting link does not exist.")
    return "link", link
