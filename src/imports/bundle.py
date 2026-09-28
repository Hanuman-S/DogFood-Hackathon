"""The event bundle: one zip that carries a whole event to another install (export here; import in
imports/bundle_import.py). The format is documented field by field in DATA-MODEL.md ("Event bundle").

    manifest.json   {"format": "dogfood-event-bundle", "version": 1, "generator", "created_at",
                     "source_event", "files": {path: sha256}}  -- every other file, and only those
    event.json      the event and everything in it, rows in pk order, each with a bundle id
                    ("projects#3"); references between rows use bundle ids
    media/<sha256>.<ext>   thumbnails and gallery images, named by their content

Never in a bundle: password hashes, sessions, API tokens, invites (judge and organizer, with their
digests), team invite tokens, voter links (nonces and digests), the vote's ballot secret and open-link
nonce, any IP hash, audit user agents, SECRET_KEY or anything derived from it. Voters are
pseudonymised: each ballot's voter -- and the same voter in the voting audit rows -- becomes
"v_" + HMAC(a random salt made for this one export and then discarded, voter identity), so ballots
from one voter can be grouped within the bundle but not tied to anyone, and two exports of the same
event give different pseudonyms. A user who appears only as a voter is not in `users` at all.

Ids inside JSON columns (results, tallies, assignment summaries) are rewritten to bundle ids through
imports/bundle_ids.py, which fails the export on an id in an undeclared place.

The export is one consistent read (REPEATABLE READ, like organizer/export.py), written to a temporary
file, refused if it would exceed the import's caps (the import could not take it back), and audited.
"""

import hashlib
import hmac
import json
import os
import secrets
import tempfile
import zipfile
from dataclasses import dataclass, field
from decimal import Decimal

from django.apps import apps
from django.conf import settings
from django.db import connection, models, transaction
from django.db.models import Q

from accounts.roles import is_organizer_of
from core import audit
from core.deadlines import db_now
from core.models import AuditAction, AuditLog

from . import bundle_ids

FORMAT = "dogfood-event-bundle"
VERSION = 1
GENERATOR = "dogfood-portal event bundle writer 1"
ZIP_TIME = (1980, 1, 1, 0, 0, 0)  # fixed, so the same content gives the same zip bytes

# Voting audit actions whose actor is the voter: the actor and detail's voter are pseudonymised.
VOTER_ACTIONS = {
    AuditAction.BALLOT_OPENED, AuditAction.VOTE_CAST, AuditAction.VOTE_CHANGED, AuditAction.VOTE_REFUSED,
    AuditAction.VOTE_LATE_REFUSED, AuditAction.VOTE_THROTTLED,
}
# Voting audit actions whose detail names a voter (an organizer acted on their ballot or link).
VOTER_DETAIL_ACTIONS = {
    AuditAction.BALLOT_VOIDED, AuditAction.BALLOT_VOID_REFUSED, AuditAction.BALLOT_RESTORED,
    AuditAction.BALLOT_RESTORE_REFUSED, AuditAction.VOTER_LINK_REVOKED, AuditAction.VOTER_LINKS_ADDED,
}
# Keys dropped from every exported audit detail, at any depth: secrets, partial secrets, addresses.
# Matched on whole "_"-separated segments of the lower-cased key, never on substrings: "ip" drops
# `ip`, `ip_hash` and `created_ip_hash` but keeps `description`, `recipient` and `skip`; "token" drops
# `token_prefix` but keeps `tokenizer`. `user_agent` is matched as the two-segment pair.
SCRUBBED_SEGMENTS = frozenset({"token", "tokens", "prefix", "digest", "secret", "nonce", "password", "ip"})
SCRUBBED_PAIRS = frozenset({("user", "agent")})


def scrubbed_key(key):
    segments = str(key).lower().split("_")
    return bool(SCRUBBED_SEGMENTS.intersection(segments)) or any(
        pair == tuple(segments[i:i + 2]) for pair in SCRUBBED_PAIRS for i in range(len(segments) - 1))


class BundleError(Exception):
    status = 400

    def __init__(self, code, detail):
        self.code = code
        self.detail = detail
        super().__init__(f"{code}: {detail}")


class NoEvent(BundleError):
    status = 404

    def __init__(self, detail="No such event."):
        super().__init__("no_event", detail)


class ExportInsideTransaction(RuntimeError):
    pass


# --- what is exported --------------------------------------------------------------------------------

@dataclass
class Section:
    name: str             # the key in event.json, and the bundle-id prefix
    model: str            # app_label.Model
    scope: str            # the lookup from the model to the event, e.g. "event" or "project__event"
    exclude: tuple = ()   # fields never exported (secrets; hashes; replaced by a pseudonym)
    one: bool = False     # a one-to-one with the event: an object, not a list


# In dependency order (the import inserts them in this order).
SECTIONS = [
    Section("event", "events.Event", "pk", one=True),
    Section("tracks", "events.Track", "event"),
    Section("prizes", "events.Prize", "event"),
    Section("questions", "events.CustomQuestion", "event"),
    Section("memberships", "events.EventMembership", "event", exclude=("side",)),  # side is generated
    Section("judge_tracks", "events.JudgeTrack", "membership__event"),
    Section("teams", "teams.Team", "event", exclude=("invite_token",)),
    Section("team_members", "teams.TeamMember", "event"),
    Section("team_extensions", "teams.TeamExtension", "team__event"),
    Section("projects", "projects.Project", "event"),
    Section("project_images", "projects.ProjectImage", "project__event"),
    Section("answers", "projects.Answer", "project__event"),
    Section("comments", "projects.Comment", "project__event"),
    Section("criteria", "scoring.Criterion", "event"),
    Section("assignment_rounds", "scoring.AssignmentRound", "event"),
    Section("assignments", "scoring.Assignment", "project__event"),
    Section("scores", "scoring.Score", "project__event"),
    Section("score_items", "scoring.ScoreItem", "score__project__event"),
    Section("scoring_config", "scoring.EventScoringConfig", "event", one=True),
    Section("result_settings", "scoring.EventResultSettings", "event", one=True),
    Section("voting_config", "voting.VotingConfig", "event", one=True,
            exclude=("ballot_secret", "open_link_nonce")),
    Section("ballots", "voting.Ballot", "event",
            exclude=("voter_user", "voter_link", "voter_cookie", "ip_hash", "created_ip_hash")),
    Section("ballot_lines", "voting.BallotLine", "ballot__event"),
    Section("tally_snapshots", "voting.VoteTallySnapshot", "event", exclude=("imported_from",)),
    Section("result_snapshots", "scoring.ResultSnapshot", "event", exclude=("imported_from",)),
    Section("publications", "scoring.Publication", "event"),
    Section("issued_records", "records.IssuedRecord", "event"),
]
BY_MODEL = {s.model: s for s in SECTIONS}

# Never exported, whatever the section list says: a test checks each is absent from the zip bytes.
NEVER_EXPORTED_MODELS = (
    "accounts.ApiToken", "accounts.UserSession", "events.JudgeInvite", "voting.VoterLink", "sessions.Session",
)


def model_of(section):
    return apps.get_model(section.model)


def rows_of(section, event):
    model = model_of(section)
    return model.objects.filter(**{section.scope: event.pk}).order_by("pk")


# --- the writer ----------------------------------------------------------------------------------

@dataclass
class _Export:
    event: object
    salt: bytes = field(default_factory=lambda: secrets.token_bytes(32))
    ids: dict = field(default_factory=dict)      # section name -> {pk: bundle id}
    users: dict = field(default_factory=dict)    # user pk -> bundle id
    user_rows: list = field(default_factory=list)
    media: dict = field(default_factory=dict)    # bundle path -> bytes
    pseudonyms: dict = field(default_factory=dict)
    missing: dict = field(default_factory=dict)  # namespace -> {source pk: "missing-<ns>#<n>"}
    voter_labels: dict = field(default_factory=dict)  # organizer-facing label -> identity

    def bundle_id(self, section, n):
        return f"{section}#{n}"

    def ref(self, section, pk):
        if pk is None:
            return None
        try:
            return self.ids[section][pk]
        except KeyError:
            raise bundle_ids.DanglingId(f"{section} row {pk} is referenced but is not part of this event") from None

    def user(self, user_id):
        if user_id is None:
            return None
        if user_id not in self.users:
            self.users[user_id] = f"users#{len(self.users) + 1}"
        return self.users[user_id]

    def pseudonym(self, identity):
        if identity not in self.pseudonyms:
            self.pseudonyms[identity] = "v_" + hmac.new(self.salt, identity.encode(), hashlib.sha256).hexdigest()[:16]
        return self.pseudonyms[identity]

    def json_mapper(self, namespace, value, typ):
        if str(value).startswith(bundle_ids.MISSING_PREFIX):
            return value  # already a missing row's name (an imported event exported again)
        try:
            pk = int(value)
        except (TypeError, ValueError):
            raise bundle_ids.DanglingId(f"{namespace}: {value!r} is not a database id") from None
        if pk in self.ids[namespace]:
            return self.ids[namespace][pk]
        if namespace not in bundle_ids.DELETABLE:
            raise bundle_ids.DanglingId(f"{namespace} row {pk} is named in a snapshot but does not exist; "
                                        f"{namespace} rows cannot be deleted, so this is an inconsistency")
        # A row deleted after the snapshot named it: a stable name within this bundle, no source number.
        names = self.missing.setdefault(namespace, {})
        if pk not in names:
            names[pk] = f"{bundle_ids.MISSING_PREFIX}{namespace}#{len(names) + 1}"
        return names[pk]


def _ballot_identity(ballot):
    if ballot.voter_user_id:
        return f"user:{ballot.voter_user_id}"
    if ballot.voter_link_id:
        return f"link:{ballot.voter_link_id}"
    return f"cookie:{ballot.voter_cookie}"


def _plain(value):
    if isinstance(value, Decimal):
        return str(value)
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def _serialise(export, section, obj):
    model = type(obj)
    row = {"id": export.ids[section.name][obj.pk]} if not section.one else {}
    for f in model._meta.concrete_fields:
        if f.primary_key or f.name in section.exclude or isinstance(f, models.GeneratedField):
            continue
        value = getattr(obj, f.attname)
        if f.is_relation:
            target = f.related_model._meta.label
            if target == settings.AUTH_USER_MODEL:
                row[f.name] = export.user(value)
            elif target == "events.Event":
                continue  # every row belongs to the one event of the bundle
            else:
                row[f.name] = export.ref(BY_MODEL[target].name, value)
        elif isinstance(f, models.FileField):
            row[f.name] = _media(export, getattr(obj, f.name))
        elif isinstance(f, models.JSONField):
            specs = bundle_ids.TABLE.get((model._meta.label, f.name))
            where = f"{model._meta.label}.{f.name} of {section.name} row {row.get('id', section.name)}"
            row[f.name] = bundle_ids.rewrite(value, specs if specs is not None else {}, export.json_mapper, where)
        else:
            row[f.name] = _plain(value)
    for m2m in model._meta.many_to_many:
        if m2m.name == "tags":
            row["tags"] = sorted(obj.tags.values_list("name", flat=True))
    if section.name == "issued_records":
        row["record_id"] = str(obj.pk)  # signed into the payload: a record keeps its id wherever it goes
    return row


def _media(export, fieldfile):
    if not fieldfile:
        return ""
    with fieldfile.open("rb") as fh:
        data = fh.read()
    if len(data) > settings.BUNDLE_MAX_MEDIA_BYTES:
        raise BundleError("bundle_too_large", f"an image is {len(data)} bytes; the import takes at most "
                          f"{settings.BUNDLE_MAX_MEDIA_BYTES} per file")
    ext = os.path.splitext(fieldfile.name)[1].lower().lstrip(".")
    if ext not in ("jpg", "png", "webp"):
        raise BundleError("unsupported_media", f"an image of type .{ext} cannot be carried in a bundle")
    path = f"media/{hashlib.sha256(data).hexdigest()}.{ext}"
    export.media[path] = data
    return path


def _scrub(detail):
    if isinstance(detail, dict):
        return {k: _scrub(v) for k, v in detail.items() if not scrubbed_key(k)}
    if isinstance(detail, list):
        return [_scrub(v) for v in detail]
    return detail


def _audit_rows(export, event):
    rows = []
    entries = AuditLog.objects.filter(Q(subject=event.slug) | Q(detail__event=event.slug)).order_by("pk")
    for n, entry in enumerate(entries, start=1):
        detail = _scrub(entry.detail or {})
        if entry.action in VOTER_ACTIONS:
            # The voter acted: no user reference at all (a voter-only account must not reach
            # `users`), and the same pseudonym as their ballot.
            identity = f"user:{entry.actor_id}" if entry.actor_id else \
                export.voter_labels.get(detail.get("voter"), f"label:{detail.get('voter', '')}")
            actor_email = export.pseudonym(identity)
            if "voter" in detail:
                detail["voter"] = actor_email
            rows.append(_audit_row(n, entry, None, actor_email, detail))
            continue
        actor, actor_email = export.user(entry.actor_id), entry.actor_email
        if entry.action in VOTER_DETAIL_ACTIONS:
            for key in ("voter", "email"):
                if key in detail:
                    label = detail[key] if key == "voter" else f"link:{detail[key]}"
                    detail[key] = export.pseudonym(export.voter_labels.get(label, f"label:{label}"))
        rows.append(_audit_row(n, entry, actor, actor_email, detail))
    return rows


def _audit_row(n, entry, actor, actor_email, detail):
    return {"id": f"audit#{n}", "created_at": entry.created_at.isoformat(), "action": entry.action,
            "actor": actor, "actor_email": actor_email, "subject": entry.subject, "detail": detail}


def build(event):
    """event.json (as a dict) and the media files. Runs inside the caller's consistent read."""
    from voting.services import ballot_label

    export = _Export(event)
    for section in SECTIONS:
        pks = list(rows_of(section, event).values_list("pk", flat=True))
        # Numbered 1..n in pk order: the import inserts in this order, so a re-export of the imported
        # event numbers every row the same, and no source primary key travels in the bundle.
        export.ids[section.name] = {pk: export.bundle_id(section.name, n) for n, pk in enumerate(pks, start=1)}

    body = {"format": FORMAT, "version": VERSION}
    for section in SECTIONS:
        objs = list(rows_of(section, event))
        if section.name == "ballots":
            for ballot in objs:
                export.voter_labels[ballot_label(ballot)] = _ballot_identity(ballot)
                if ballot.voter_cookie:
                    # An imported event's voters are pseudonyms stored as cookies, and its audit rows name
                    # them by that pseudonym: map it to the same identity, so ballot and rows stay grouped.
                    export.voter_labels[ballot.voter_cookie] = _ballot_identity(ballot)
        rows = []
        for obj in objs:
            row = _serialise(export, section, obj)
            if section.name == "ballots":
                row["voter"] = export.pseudonym(_ballot_identity(obj))
            rows.append(row)
        body[section.name] = (rows[0] if rows else None) if section.one else rows

    # Users referenced so far (never by a ballot: its voter is a pseudonym). Audit rows may add more.
    body["fixture_refs"] = _fixture_refs(export, event)
    body["signing_keys"] = _signing_keys(event)
    body["audit"] = _audit_rows(export, event)
    from accounts.models import User
    people = {u.pk: u for u in User.objects.filter(pk__in=list(export.users))}
    body["users"] = [{"id": bundle_id, "email": people[pk].email, "name": people[pk].name}
                     for pk, bundle_id in sorted(export.users.items(), key=lambda kv: int(kv[1].split("#")[1]))]
    return body, export


def _signing_keys(event):
    """The PUBLIC halves of the keys the event's records were signed with (this install's own, and any
    other install's the records came with), so the records can be verified wherever the bundle goes.
    Never a private key: those are files in the secrets volume, never read here."""
    from records.models import ForeignSigningKey, IssuedRecord, SigningKey

    kids = set(IssuedRecord.objects.filter(event=event).values_list("kid", flat=True))
    rows = list(SigningKey.objects.filter(kid__in=kids)) + list(
        ForeignSigningKey.objects.filter(kid__in=kids).exclude(kid__in=[k.kid for k in SigningKey.objects.all()]))
    return sorted(({"kid": k.kid, "alg": k.alg, "public_key": k.public_key,
                    "created_at": k.created_at.isoformat() if k.created_at else "",
                    "retired_at": k.retired_at.isoformat() if k.retired_at else ""} for k in rows),
                  key=lambda k: k["kid"])


def _fixture_refs(export, event):
    """The event's fixture refs that scoring reads (kind project and score), selected by (kind,
    object_id) -- never by object_id alone, which could be another kind's row with the same number."""
    from imports.models import FixtureRef

    rows = []
    for kind, section in (("project", "projects"), ("score", "scores")):
        wanted = export.ids[section]
        for ref in FixtureRef.objects.filter(kind=kind, object_id__in=list(wanted)).order_by("pk"):
            rows.append({"kind": kind, "object": wanted[ref.object_id], "source": ref.source,
                         "external_id": ref.external_id, "duplicate_of": ref.duplicate_of, "note": ref.note,
                         "created_at": ref.created_at.isoformat()})
    return rows


def encode(body):
    return json.dumps(body, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _consistent_read():
    if connection.in_atomic_block:
        raise ExportInsideTransaction("the bundle export opens its own REPEATABLE READ transaction; call it "
                                      "outside transaction.atomic (views: @transaction.non_atomic_requests)")
    atomic = transaction.atomic()
    atomic.__enter__()
    if connection.vendor == "postgresql":
        with connection.cursor() as cursor:
            cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
    return atomic


def export_event(event, *, actor, origin=None):
    """Write the bundle of `event` to a temporary file and return its path (the caller deletes it).
    `actor` must organize the event (or be a platform admin); None means the operator on the host
    (manage.py export_event). Refusals raise BundleError (audited)."""

    def refuse(error):
        audit.record(AuditAction.EVENT_EXPORT_REFUSED, origin=origin, actor=actor, subject=event.slug,
                     event=event.slug, reason=error.code, detail_text=error.detail[:300])
        raise error

    if actor is not None and not is_organizer_of(actor, event):
        refuse(NoEvent())
    now = db_now()
    atomic = _consistent_read()
    try:
        body, export = build(event)
    except bundle_ids.UnknownIdField as error:
        atomic.__exit__(None, None, None)
        refuse(BundleError("unknown_id_field", str(error)))
    except bundle_ids.DanglingId as error:
        atomic.__exit__(None, None, None)
        refuse(BundleError("dangling_id", str(error)))
    except BundleError as error:
        atomic.__exit__(None, None, None)
        refuse(error)
    except BaseException as error:
        atomic.__exit__(type(error), error, error.__traceback__)
        raise
    else:
        atomic.__exit__(None, None, None)

    files = {"event.json": encode(body), **export.media}
    too_big = _over_caps(files)
    if too_big:
        refuse(BundleError("bundle_too_large", too_big))
    manifest = {
        "format": FORMAT, "version": VERSION, "generator": GENERATOR, "created_at": now.isoformat(),
        "source_event": event.slug,
        "files": {path: hashlib.sha256(data).hexdigest() for path, data in sorted(files.items())},
    }
    files["manifest.json"] = encode(manifest)
    handle, path = tempfile.mkstemp(prefix="dogfood-bundle-", suffix=".zip")
    os.close(handle)
    try:
        with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for name in sorted(files):
                info = zipfile.ZipInfo(name, date_time=ZIP_TIME)
                info.compress_type = zipfile.ZIP_DEFLATED
                info.external_attr = 0o644 << 16
                archive.writestr(info, files[name])
        size = os.path.getsize(path)
        if size > settings.BUNDLE_MAX_BYTES:
            raise BundleError("bundle_too_large", f"the bundle is {size} bytes; the import takes at most "
                              f"{settings.BUNDLE_MAX_BYTES}")
        with open(path, "rb") as fh:
            digest = hashlib.file_digest(fh, "sha256").hexdigest()
    except BundleError as error:
        os.unlink(path)
        refuse(error)
    except BaseException:
        os.unlink(path)
        raise
    audit.record(AuditAction.EVENT_EXPORTED, origin=origin, actor=actor, subject=event.slug, event=event.slug,
                 sha256=digest, bytes=size, media=len(export.media),
                 missing={ns: len(names) for ns, names in export.missing.items()},
                 rows={s.name: (1 if body[s.name] else 0) if s.one else len(body[s.name]) for s in SECTIONS})
    return path


def _over_caps(files):
    """A reason if the import's caps would refuse these files (so they are never written), else ""."""
    if len(files) + 1 > settings.BUNDLE_MAX_ENTRIES:
        return f"{len(files) + 1} files; the import takes at most {settings.BUNDLE_MAX_ENTRIES}"
    if len(files["event.json"]) > settings.BUNDLE_MAX_EVENT_JSON_BYTES:
        return (f"event.json is {len(files['event.json'])} bytes; the import takes at most "
                f"{settings.BUNDLE_MAX_EVENT_JSON_BYTES}")
    total = sum(len(data) for data in files.values())
    if total > settings.BUNDLE_MAX_TOTAL_BYTES:
        return f"{total} bytes uncompressed; the import takes at most {settings.BUNDLE_MAX_TOTAL_BYTES}"
    return ""


def filename(event, now=None):
    now = now or db_now()
    return f"{event.slug}-bundle-{now:%Y%m%d-%H%M%S}.zip"
