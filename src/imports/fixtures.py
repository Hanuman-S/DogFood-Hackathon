"""Import the organizers' shared dataset (acceptance/fixtures.json).

The file is *input*, not our data model: this module maps it onto our schema and says exactly
what it did. Rules:

* **Create-only and idempotent.** Every created row gets a FixtureRef. A second run finds the
  refs and creates nothing; it never overwrites a row an organizer has since edited.
* **All or nothing.** The whole import is one transaction.
* **Honest about the data.** The file has deliberate edge cases. Each one is reported, not
  silently smoothed over:
    - a team that submitted twice -> the first submission is kept, the second is recorded as a
      duplicate of it (both external ids resolve to the same project, so scores for either land
      in one place when judging arrives);
    - a person who is both a judge and a team member -> kept as a judge, left off the team, and
      reported as a conflict of interest (our schema cannot let staff compete);
    - a person on two teams in one event -> kept on the first, reported.
* **Past the deadline, on purpose.** The fixture event closed long ago, so its rows are written
  inside `deadline_bypass()` -- the same audited door organizer tools use.

Dates the file does not give (open, start, judging end) are derived from the ones it does and
listed in the report.
"""

import json
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

from django.conf import settings
from django.db import transaction
from django.utils.text import slugify

from accounts.models import User, normalize_email
from accounts.roles import Role
from core import audit
from core.deadlines import deadline_bypass
from core.models import AuditAction
from events.models import Event, EventOrganizer, Track
from imports.models import FixtureRef
from projects.models import Project, Status
from teams.models import Team, TeamMember

SOURCE = "dogfood-fixtures"


class FixtureError(Exception):
    """The file is unusable; nothing was imported."""


@dataclass
class Report:
    created: dict = field(default_factory=dict)
    existing: dict = field(default_factory=dict)
    duplicates: list = field(default_factory=list)
    conflicts: list = field(default_factory=list)
    derived: list = field(default_factory=list)

    def count(self, bucket, kind):
        getattr(self, bucket)[kind] = getattr(self, bucket).get(kind, 0) + 1

    def lines(self):
        kinds = ["event", "track", "user", "team", "project"]
        out = ["kind      created  already there"]
        for kind in kinds:
            out.append(f"{kind:<9} {self.created.get(kind, 0):>7}  {self.existing.get(kind, 0):>13}")
        for line in self.derived:
            out.append(f"derived:   {line}")
        for line in self.duplicates:
            out.append(f"duplicate: {line}")
        for line in self.conflicts:
            out.append(f"conflict:  {line}")
        return out


def parse_time(value):
    return datetime.fromisoformat(str(value).replace("Z", "+00:00"))


def load(path):
    path = Path(path)
    if not path.exists():
        raise FixtureError(f"No fixture file at {path}.")
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise FixtureError(f"{path} is not valid JSON: {error}") from error
    for key in ("event", "tracks", "judges", "teams", "projects"):
        if key not in data:
            raise FixtureError(f"{path} has no '{key}' section.")
    return data


class Importer:
    def __init__(self, data, report=None):
        self.data = data
        self.report = report or Report()
        self.password = settings.DEMO_PASSWORD if settings.DEMO_MODE else None

    # --- refs -----------------------------------------------------------------------------

    def ref(self, kind, external_id):
        return FixtureRef.objects.filter(source=SOURCE, kind=kind, external_id=external_id).first()

    def remember(self, kind, external_id, obj, **extra):
        FixtureRef.objects.create(source=SOURCE, kind=kind, external_id=external_id, object_id=obj.pk, **extra)

    # --- the import -------------------------------------------------------------------------

    def run(self):
        with deadline_bypass(None, "fixture import"):
            with transaction.atomic():
                event = self.import_event()
                tracks = self.import_tracks(event)
                self.import_judges()
                teams = self.import_teams(event)
                self.import_projects(event, tracks, teams)
        audit.record(
            AuditAction.FIXTURES_IMPORTED, subject=SOURCE,
            created=self.report.created, duplicates=len(self.report.duplicates),
            conflicts=len(self.report.conflicts),
        )
        return self.report

    def import_event(self):
        raw = self.data["event"]
        ref = self.ref("event", raw["id"])
        if ref:
            self.report.count("existing", "event")
            return Event.objects.get(pk=ref.object_id)

        close = parse_time(raw["submissions_close"])
        submitted = [parse_time(p["submitted_at"]) for p in self.data["projects"] if p.get("submitted_at")]
        opens = min([close - timedelta(hours=72)] + [t - timedelta(hours=1) for t in submitted])
        judging_ends = close + timedelta(days=14)
        self.report.derived += [
            f"submissions open {opens:%Y-%m-%d %H:%M} UTC (72 h before the close, or before the first submission)",
            f"judging ends {judging_ends:%Y-%m-%d %H:%M} UTC (14 days after the close)",
        ]
        slug = base = slugify(raw.get("name") or raw["id"])[:50] or "imported-event"
        n = 2
        while Event.objects.filter(slug=slug).exists():
            slug, n = f"{base}-{n}", n + 1
        sizes = [len(t.get("members") or []) for t in self.data["teams"]] or [4]
        event = Event.objects.create(
            slug=slug,
            name=raw.get("name") or raw["id"],
            tagline="Imported from the organizers' shared dataset.",
            starts_at=opens,
            submissions_open_at=opens,
            submissions_close_at=close,
            judging_ends_at=judging_ends,
            min_team_size=1,
            max_team_size=min(20, max(sizes)),
            is_published=True,
        )
        # In demo mode, let the demo organizer manage it; otherwise only admins can, until an
        # admin adds an organizer.
        organizer = User.objects.filter(email="organizer@dogfood.local", role=Role.ORGANIZER).first()
        if settings.DEMO_MODE and organizer:
            EventOrganizer.objects.create(event=event, user=organizer, added_by=None)
        self.remember("event", raw["id"], event)
        self.report.count("created", "event")
        return event

    def import_tracks(self, event):
        tracks = {}
        for order, raw in enumerate(self.data["tracks"], start=1):
            ref = self.ref("track", raw["id"])
            if ref:
                tracks[raw["id"]] = Track.objects.get(pk=ref.object_id)
                self.report.count("existing", "track")
                continue
            track = Track.objects.create(event=event, name=raw.get("name") or raw["id"], order=order)
            self.remember("track", raw["id"], track)
            tracks[raw["id"]] = track
            self.report.count("created", "track")
        return tracks

    def user(self, email, name, role):
        email = normalize_email(email)
        user = User.objects.filter(email=email).first()
        if user:
            self.report.count("existing", "user")
            return user
        user = User.objects.create_user(email, self.password, name=name, role=role)
        self.remember("user", email, user)
        self.report.count("created", "user")
        return user

    def import_judges(self):
        for raw in self.data["judges"]:
            self.user(raw["email"], raw.get("name") or raw["email"].split("@")[0], Role.JUDGE)

    def import_teams(self, event):
        teams = {}
        for raw in self.data["teams"]:
            ref = self.ref("team", raw["id"])
            if ref:
                teams[raw["id"]] = Team.objects.get(pk=ref.object_id)
                self.report.count("existing", "team")
                continue
            members = []
            for email in raw.get("members") or []:
                person = self.user(email, email.split("@")[0], Role.PARTICIPANT)
                if person.role != Role.PARTICIPANT:
                    self.report.conflicts.append(
                        f"{person.email} is a {person.role} and a member of team {raw['id']}: "
                        "kept as staff, left off the team (conflict of interest)"
                    )
                    continue
                other = TeamMember.objects.filter(event=event, user=person).select_related("team").first()
                if other:
                    self.report.conflicts.append(
                        f"{person.email} is on two teams ({other.team.name} and {raw['id']}): kept on the first"
                    )
                    continue
                members.append(person)
            if not members:
                self.report.conflicts.append(f"team {raw['id']} has no importable members: skipped")
                continue
            name = raw.get("name") or raw["id"]
            if Team.objects.filter(event=event, name__iexact=name).exists():
                name = f"{name} ({raw['id']})"
            team = Team.objects.create(event=event, name=name, captain=members[0])
            for person in members:
                TeamMember.objects.create(team=team, user=person)
            self.remember("team", raw["id"], team)
            teams[raw["id"]] = team
            self.report.count("created", "team")
        return teams

    def import_projects(self, event, tracks, teams):
        # Oldest first, so when a team submitted twice the first submission is the one kept.
        rows = sorted(self.data["projects"], key=lambda p: (p.get("submitted_at") or "", p["id"]))
        for raw in rows:
            if self.ref("project", raw["id"]):
                self.report.count("existing", "project")
                continue
            team = teams.get(raw.get("team"))
            if team is None:
                self.report.conflicts.append(f"project {raw['id']} belongs to unknown team {raw.get('team')}: skipped")
                continue
            existing = Project.objects.filter(team=team).first()
            if existing:
                first = FixtureRef.objects.filter(source=SOURCE, kind="project", object_id=existing.pk).first()
                first_id = first.external_id if first else f"project #{existing.pk}"
                note = (
                    f"{raw['id']} ({raw.get('title')}) is a second submission by team {raw.get('team')}; "
                    f"recorded as a duplicate of {first_id}, which is kept"
                )
                self.remember("project", raw["id"], existing, duplicate_of=first_id, note=note[:300])
                self.report.duplicates.append(note)
                continue
            submitted_at = parse_time(raw["submitted_at"]) if raw.get("submitted_at") else event.submissions_close_at
            project = Project.objects.create(
                team=team,
                name=(raw.get("title") or raw["id"])[:120],
                tagline=(raw.get("summary") or "")[:200],
                repo_url=raw.get("repo_url") or "",
                track=tracks.get(raw.get("track")),
                status=Status.SUBMITTED,
                submitted_at=submitted_at,
            )
            self.remember("project", raw["id"], project)
            self.report.count("created", "project")


def import_file(path=None):
    return Importer(load(path or settings.FIXTURES_PATH)).run()
