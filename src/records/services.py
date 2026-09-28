"""Issuing, revoking and verifying signed records (C2). Every write is here, audited, refusals too;
services take the actor and an `audit.Origin`, never the request.

Payload (canonical JSON, records/canonical.py), for every kind:
    v 1, kind, record_id, issued_at (ISO, UTC, "Z"), kid,
    event {slug, name, submissions_open_at, submissions_close_at}, subject {name}  -- a display name, no email
    (the submission window, not judging's end: the timeline freeze keeps it fixed once judging has work,
    so extending judging reissues judge records only, whose own judging window did change)
and by kind:
    judge_participation  reviews_submitted, judging {starts_at, ends_at}     -- never a score
    participant          team, project
    winner               place, track ("" = overall), peoples_choice ("yes"/"no"), team, project

Who gets what, and when (organizers of the event and admins issue; 404 for anyone else):
* judge records: once judging has closed (409 judging_open before), to each judge with at least one
  submitted review;
* participant records: once submissions have closed (409 submissions_open), to each person who is a
  member of a team whose project is submitted, at the moment the records are issued. Someone who left
  before the close is not a member, so gets none. Nobody can leave after the close (the service and the
  deadline trigger refuse it), except through an organizer's audited deadline bypass or a team extension
  that ends before judging; a person removed that way has their record revoked on the next issue (no
  longer eligible);
* winner records: only from the event's active published final (409 no_published_final), to each
  member of each winning team, one record per win: (track, place, People's Choice) is the slot.

Issuing is idempotent by meaning, not by bytes: for each (subject, kind, slot) the record that would be
issued now is compared with the active one on everything but record_id, issued_at and kid. The same:
skipped. Different: the old one is revoked ("reissued") and a new one signed. A subject who is no longer
eligible (a winner no longer winning, a judge with no submitted review after judging reopened) has their
active record of that kind revoked ("no longer eligible"). The event row is locked while this runs.
"""

from dataclasses import dataclass

from django.db import transaction

from accounts.roles import Role, is_organizer_of
from core import audit, ratelimit
from core.deadlines import db_now
from core.models import AuditAction

from . import keys
from .canonical import CanonicalError, canonical
from .errors import (
    AlreadyRevoked, InvalidKind, InvalidRevoke, JudgingOpen, NoEvent, NoPublishedFinal, NoRecord, RateLimited,
    SigningUnavailable, SubmissionsOpen,
)
from .models import ForeignSigningKey, IssuedRecord, RecordKind, RevokeCategory, SigningKey, winner_slot

REVOKE_REASON_MAX = 300
VOLATILE = ("record_id", "issued_at", "kid")


def _iso(value):
    return value.strftime("%Y-%m-%dT%H:%M:%SZ") if value else ""


def _event_block(event):
    """The event's identity and its submission window: dates the timeline freeze makes immutable once
    judging has work, so a record's meaning never changes because judging was extended."""
    return {"slug": event.slug, "name": event.name, "submissions_open_at": _iso(event.submissions_open_at),
            "submissions_close_at": _iso(event.submissions_close_at)}


@dataclass
class Wanted:
    user: object
    slot: str
    fields: dict  # the kind's own fields


# --- who should hold which record now ---------------------------------------------------------------------

def _judges(event, now):
    from scoring.services import judging_closed
    from events.models import EventMembership
    from scoring.models import Score

    if not judging_closed(event, now):
        raise JudgingOpen(f"Judging closes at {_iso(event.judging_ends_at)}; judge records are issued after that.")
    out = []
    for m in EventMembership.objects.filter(event=event, role=Role.JUDGE).select_related("user").order_by("pk"):
        n = Score.objects.filter(judge=m, submitted_at__isnull=False).count()
        if n:
            out.append(Wanted(m.user, "", {"reviews_submitted": n, "judging": {
                "starts_at": _iso(event.judging_starts_at), "ends_at": _iso(event.judging_ends_at)}}))
    return out


def _participants(event, now):
    from projects.models import Project, Status

    if now < event.submissions_close_at:
        raise SubmissionsOpen("Submissions are still open; participant records are issued after they close.")
    out = []
    for project in (Project.objects.filter(event=event, status=Status.SUBMITTED).select_related("team")
                    .order_by("pk")):
        for member in project.team.members.select_related("user").order_by("pk"):
            out.append(Wanted(member.user, "", {"team": project.team.name, "project": project.name}))
    return out


def _winners(event, now):
    from scoring import results

    publication = results.active_publication(event)
    if publication is None or publication.snapshot.kind != "final":
        raise NoPublishedFinal("Winner records come only from a published final result: publish one first.")
    snapshot = publication.snapshot
    rows, _ = results.build_rows(event, snapshot)
    top_n = results.result_settings(event).winners_top_n
    overall, tracks = results.winners(rows, top_n)
    wins = [("", r.display_rank, False, r.project) for r in overall]
    wins += [(t.track.name, 1, False, r.project) for t in tracks for r in t.rows]
    wins += [("", c.rank, True, c.project) for c in results.peoples_choice(event, snapshot, top_n=top_n) or ()]
    out = []
    for track, place, choice, project in wins:
        for member in project.team.members.select_related("user").order_by("pk"):
            out.append(Wanted(member.user, winner_slot(track, place, choice), {
                "place": int(place), "track": track, "peoples_choice": "yes" if choice else "no",
                "team": project.team.name, "project": project.name}))
    return out


WANTED = {RecordKind.JUDGE: _judges, RecordKind.PARTICIPANT: _participants, RecordKind.WINNER: _winners}


def _meaning(payload):
    return {k: v for k, v in payload.items() if k not in VOLATILE}


# --- issuing and revoking ------------------------------------------------------------------------------------

def issue_records(event, kind, *, actor, origin=None):
    """Issue (or bring up to date) every record of `kind` in `event`. Returns counts
    {"issued", "reissued", "unchanged", "revoked"}."""

    def refuse(error, why):
        audit.record(AuditAction.RECORD_ISSUE_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     kind=str(kind), reason=why)
        raise error

    if actor is None or not is_organizer_of(actor, event):
        refuse(NoEvent("No such event."), "not an organizer")
    if kind not in WANTED:
        refuse(InvalidKind(f"kind must be one of {', '.join(k.value for k in RecordKind)}."), "invalid kind")
    from events.models import Event

    counts = {"issued": 0, "reissued": 0, "unchanged": 0, "revoked": 0}
    written, superseded, not_eligible = [], [], []
    try:
        with transaction.atomic():
            Event.objects.select_for_update().filter(pk=event.pk).first()
            now = db_now()
            wanted = WANTED[kind](event, now)
            active = {(r.subject_user_id, r.slot): r
                      for r in IssuedRecord.objects.select_for_update().filter(event=event, kind=kind,
                                                                               revoked_at__isnull=True,
                                                                               is_foreign=False)}
            seen = set()
            for want in wanted:
                key = (want.user.pk, want.slot)
                if key in seen:
                    continue  # the same win listed twice (a tie on the cut): one record
                seen.add(key)
                payload = _payload(event, kind, want)
                old = active.pop(key, None)
                if old is not None and _meaning(old.payload) == _meaning(payload):
                    counts["unchanged"] += 1
                    continue
                if old is not None:
                    _revoke(old, "reissued", actor, now, RevokeCategory.SUPERSEDED)
                    superseded.append(str(old.pk))
                    counts["reissued"] += 1
                else:
                    counts["issued"] += 1
                written.append(_sign_and_store(event, kind, want, payload, actor, now))
            for gone in active.values():  # held a record, no longer eligible
                _revoke(gone, "no longer eligible", actor, now, RevokeCategory.NOT_ELIGIBLE)
                not_eligible.append(str(gone.pk))
                counts["revoked"] += 1
    except (JudgingOpen, SubmissionsOpen, NoPublishedFinal) as error:
        refuse(error, error.code)
    except SigningUnavailable as error:
        refuse(error, error.code)
    for record in written:
        audit.record(AuditAction.RECORD_ISSUED, origin=origin, actor=actor, subject=event.slug,
                     record=str(record.id), kind=record.kind, email=record.subject_user.email, kid=record.kid)
    if superseded or not_eligible:
        audit.record(AuditAction.RECORD_REVOKED, origin=origin, actor=actor, subject=event.slug, kind=str(kind),
                     reason="reissue", superseded=superseded, no_longer_eligible=not_eligible)
    return counts


def _payload(event, kind, want):
    import uuid

    return {"v": 1, "kind": str(kind), "record_id": str(uuid.uuid4()), "issued_at": "", "kid": "",
            "event": _event_block(event), "subject": {"name": want.user.name}, **want.fields}


def _sign_and_store(event, kind, want, payload, actor, now):
    row = keys.active()
    payload = dict(payload, issued_at=_iso(now), kid=row.kid if row else "")
    try:
        text = canonical(payload)
    except CanonicalError as error:  # a programming error: never sign something another verifier reads differently
        raise ValueError(str(error)) from error
    kid, signature = keys.sign(text)
    if kid != payload["kid"]:  # rotated between the two reads: sign again with the payload naming it
        payload["kid"] = kid
        text = canonical(payload)
        kid, signature = keys.sign(text)
    return IssuedRecord.objects.create(
        id=payload["record_id"], kind=kind, slot=want.slot, event=event, subject_user=want.user, payload=payload,
        payload_text=text.decode("utf-8"), signature=signature, kid=kid, issued_by=actor, issued_at=now)


def _revoke(record, reason, actor, now, category):
    IssuedRecord.objects.filter(pk=record.pk).update(revoked_at=now, revoked_by=actor, revoke_reason=reason,
                                                     revoke_category=category)


def replacement(record):
    """For a superseded record, the record that replaced it (the next one in the same slot)."""
    if record.revoke_category != RevokeCategory.SUPERSEDED:
        return None
    return (IssuedRecord.objects.filter(event_id=record.event_id, subject_user_id=record.subject_user_id,
                                        kind=record.kind, slot=record.slot, issued_at__gte=record.revoked_at)
            .exclude(pk=record.pk).order_by("issued_at").first())


def revoke_record(record_id, reason, *, actor, origin=None):
    record = IssuedRecord.objects.select_related("event").filter(pk=record_id).first() if _uuid(record_id) else None

    def refuse(error, why):
        audit.record(AuditAction.RECORD_REVOKE_REFUSED, origin=origin, actor=actor,
                     subject=record.event.slug if record else "", record=str(record_id), reason=why)
        raise error

    if record is None or actor is None or not is_organizer_of(actor, record.event):
        refuse(NoRecord("No such record."), "no such record" if record is None else "not an organizer")
    reason = (reason or "").strip() if isinstance(reason, str) else ""
    if not reason or len(reason) > REVOKE_REASON_MAX:
        refuse(InvalidRevoke(f"Give a reason (1 to {REVOKE_REASON_MAX} characters); it is shown on the record."),
               "invalid reason")
    with transaction.atomic():
        fresh = IssuedRecord.objects.select_for_update().get(pk=record.pk)
        already = fresh.revoked_at is not None
        if not already:
            _revoke(fresh, reason, actor, db_now(), RevokeCategory.ORGANIZER)
    if already:
        refuse(AlreadyRevoked("This record is already revoked."), "already revoked")
    audit.record(AuditAction.RECORD_REVOKED, origin=origin, actor=actor, subject=record.event.slug,
                 record=str(record.pk), reason=reason)
    return IssuedRecord.objects.get(pk=record.pk)


def _uuid(value):
    import uuid
    try:
        uuid.UUID(str(value))
    except ValueError:
        return False
    return True


# --- verifying ---------------------------------------------------------------------------------------------

@dataclass
class Verdict:
    state: str   # valid | invalid | revoked | unknown_key | foreign_valid | foreign_invalid | not_canonical
    kid: str = ""
    record: object = None
    label: str = ""


LABELS = {
    "valid": "valid: signed by this install",
    "invalid": "invalid: the signature does not match these exact bytes",
    "revoked": "revoked",
    "unknown_key": "unknown key: no key with this kid was ever published here",
    "foreign_valid": "signed by another install (kid {kid})",
    "foreign_invalid": "invalid: the signature does not match (a key of another install, kid {kid})",
    "not_canonical": "not a canonical payload: re-serialise it (sorted keys, no whitespace, UTF-8)",
}


def _verdict(state, kid="", record=None):
    return Verdict(state, kid, record, LABELS[state].format(kid=kid))


def verify_bytes(text: bytes, signature: str):
    """Verify exactly `text` (which must be canonical) against `signature`: the kid inside says whose key."""
    import json

    try:
        payload = json.loads(text.decode("utf-8"))
        ok = isinstance(payload, dict) and canonical(payload) == text
    except (ValueError, CanonicalError, UnicodeDecodeError):
        ok = False
    if not ok:
        return _verdict("not_canonical")
    kid = payload.get("kid") if isinstance(payload.get("kid"), str) else ""
    record = IssuedRecord.objects.filter(pk=payload.get("record_id")).first() if _uuid(payload.get("record_id")) \
        else None
    own = SigningKey.objects.filter(kid=kid).first()
    if own is not None:
        if not keys.verify(own.public_key, text, signature):
            return _verdict("invalid", kid, record)
        if record is not None and record.revoked_at is not None and record.payload_text.encode() == text:
            return _verdict("revoked", kid, record)
        return _verdict("valid", kid, record)
    foreign = ForeignSigningKey.objects.filter(kid=kid).first()
    if foreign is not None:
        good = keys.verify(foreign.public_key, text, signature)
        if good and record is not None and record.revoked_at is not None:
            return _verdict("revoked", kid, record)
        return _verdict("foreign_valid" if good else "foreign_invalid", kid, record)
    return _verdict("unknown_key", kid, record)


def verify_record(record):
    """The server-side check shown on a record's page."""
    return verify_bytes(record.payload_text.encode("utf-8"), record.signature)


VERIFY_ACTIONS = (AuditAction.RECORD_VERIFY,)


def verify_submission(text, signature, *, origin=None):
    """/verify: one audited check per attempt, limited per IP hash (counted from live audit rows)."""
    from django.conf import settings

    which = ratelimit.exceeded(VERIFY_ACTIONS, now=db_now(), per_ip=ratelimit.Limit(
        settings.VERIFY_RATE_PER_IP, settings.VERIFY_RATE_WINDOW), ip_hash=origin.ip_hash if origin else "")
    if which:
        audit.record(AuditAction.RECORD_VERIFY_THROTTLED, origin=origin, limit=which)
        raise RateLimited("Too many checks from this network. Try again in a few minutes.")
    text = (text or "").strip().encode("utf-8")
    verdict = verify_bytes(text, (signature or "").strip())
    audit.record(AuditAction.RECORD_VERIFY, origin=origin, result=verdict.state, kid=verdict.kid,
                 record=str(verdict.record.pk) if verdict.record else "")
    return verdict
