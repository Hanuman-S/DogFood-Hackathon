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
      reported as a conflict of interest (nobody may be staff and compete in one event);
    - a person on two teams in one event -> kept on the first, reported;
    - a judge who reviewed both a submission and its duplicate -> the review of the submission
      that was kept is imported, the other is reported (one review per judge per project).
* **Roles are per event.** Judges become `EventMembership(judge)` rows with their tracks
  (`JudgeTrack`); team members become `EventMembership(participant)` rows. Scores hang off the
  judge's membership, so "this judge, in this event" is one column.
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
from django.utils import timezone
from django.utils.text import slugify

from accounts.models import User, normalize_email
from accounts.roles import Role, can_compete_in
from core import audit
from core.deadlines import deadline_bypass
from core.models import AuditAction
from events.models import Event, EventMembership, JudgeTrack, Track
from imports.models import FixtureRef
from projects.models import Project, Status
from scoring.services import split_equally
from scoring.models import Assignment, AssignmentSource, Criterion, Score, ScoreItem
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
        kinds = ["event", "track", "user", "judge", "team", "project", "criterion", "score"]
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
                judges = self.import_judges(event, tracks)
                teams = self.import_teams(event)
                self.import_projects(event, tracks, teams)
                self.import_scores(event, judges)
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
        starts = opens - timedelta(hours=1)
        judging_starts = close + timedelta(hours=1)
        judging_ends = close + timedelta(days=14)
        self.report.derived += [
            f"event starts {starts:%Y-%m-%d %H:%M} UTC (1 h before submissions open)",
            f"submissions open {opens:%Y-%m-%d %H:%M} UTC (72 h before the close, or before the first submission)",
            f"judging starts {judging_starts:%Y-%m-%d %H:%M} UTC (1 h after the close)",
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
            description=(
                "The DOGFOOD organizers' shared fixture data (`acceptance/fixtures.json`), imported "
                "at boot so every portal is compared on the same projects, judges and reviews.\n\n"
                "The file gives only the submission close; the other dates are derived and listed "
                "in the import report."
            ),
            starts_at=starts,
            submissions_open_at=opens,
            submissions_close_at=close,
            judging_starts_at=judging_starts,
            judging_ends_at=judging_ends,
            min_team_size=1,
            max_team_size=min(20, max(sizes)),
            is_published=True,
        )
        # In demo mode, the demo organizer manages it and the two demo judges judge it (so the
        # judge_a / judge_b headers in .dogfood.toml name real judges of the fixture event).
        # Otherwise only admins can manage it until an admin makes someone its organizer.
        if settings.DEMO_MODE:
            for email, role in [
                ("organizer@dogfood.local", Role.ORGANIZER),
                ("judge.a@dogfood.local", Role.JUDGE),
                ("judge.b@dogfood.local", Role.JUDGE),
            ]:
                user = User.objects.filter(email=email).first()
                if user:
                    EventMembership.objects.create(event=event, user=user, role=role)
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

    def user(self, email, name):
        email = normalize_email(email)
        user = User.objects.filter(email=email).first()
        if user:
            self.report.count("existing", "user")
            return user
        user = User.objects.create_user(email, self.password, name=name)
        self.remember("user", email, user)
        self.report.count("created", "user")
        return user

    def import_judges(self, event, tracks):
        """Each judge becomes a judge *of this event*, covering their listed tracks.

        Returns {fixture judge id: EventMembership}, rebuilt on every run so scores can find them.
        """
        judges = {}
        for raw in self.data["judges"]:
            person = self.user(raw["email"], raw.get("name") or raw["email"].split("@")[0])
            membership, created = EventMembership.objects.get_or_create(
                user=person, event=event, role=Role.JUDGE
            )
            if created:
                for track_id in raw.get("tracks") or []:
                    if track_id in tracks:
                        JudgeTrack.objects.create(membership=membership, track=tracks[track_id])
            self.report.count("created" if created else "existing", "judge")
            # The fixture's judge id (e.g. "jdg_02") -> this judge membership, so the id can name
            # the judge later (/api/judge/scores?judge=jdg_02). Create-only: an older import gets
            # the refs on its next run.
            if self.ref("judge", raw["id"]) is None:
                self.remember("judge", raw["id"], membership)
            judges[raw["id"]] = membership
        return judges

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
                person = self.user(email, email.split("@")[0])
                if can_compete_in(person, event):
                    self.report.conflicts.append(
                        f"{person.email} is staff in this event and a member of team {raw['id']}: "
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
                EventMembership.objects.get_or_create(user=person, event=event, role=Role.PARTICIPANT)
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


    def import_scores(self, event, judges):
        """Criteria from the score keys (equal weights: the file gives none), then one submitted Score
        per review, each with the Assignment it implies.
        """
        rows = self.data.get("scores") or []
        keys = []
        for raw in rows:
            for key in raw.get("criteria") or {}:
                if key not in keys:
                    keys.append(key)
        criteria = {}
        # The file gives no weights, so every criterion counts equally: relative weight 1 each.
        weights = split_equally(len(keys))
        for order, (key, weight) in enumerate(zip(keys, weights), start=1):
            criterion, created = Criterion.objects.get_or_create(
                event=event, key=key,
                defaults={"label": key.replace("_", " ").capitalize(), "weight": weight, "order": order},
            )
            self.report.count("created" if created else "existing", "criterion")
            criteria[key] = criterion

        # Reviews of kept submissions first, so a judge's review of the original always wins
        # over their review of its duplicate, whatever order the file lists them in.
        duplicate_ids = set(
            FixtureRef.objects.filter(source=SOURCE, kind="project").exclude(duplicate_of="")
            .values_list("external_id", flat=True)
        )
        for raw in sorted(rows, key=lambda r: r.get("project") in duplicate_ids):
            membership = judges.get(raw.get("judge"))
            ref = self.ref("project", raw.get("project"))
            if membership is None or ref is None:
                self.report.conflicts.append(
                    f"review by {raw.get('judge')} of {raw.get('project')}: unknown judge or project, skipped"
                )
                continue
            external_id = f"{raw['judge']}:{raw['project']}"
            if self.ref("score", external_id):
                self.report.count("existing", "score")
                continue
            project = Project.objects.get(pk=ref.object_id)
            if Score.objects.filter(judge=membership, project=project).exists():
                self.report.duplicates.append(
                    f"review by {raw['judge']} of {raw['project']} (a duplicate of {ref.duplicate_of}): "
                    f"that judge already reviewed {ref.duplicate_of}, whose review is kept"
                )
                continue
            # A review in the file is a finished one, and it implies the judge was asked to do
            # it: so it is imported as submitted, together with the assignment behind it (in
            # file order within each judge's queue). T2's progress counts then start from truth.
            score = Score.objects.create(
                judge=membership, project=project, comment=raw.get("comment") or "",
                submitted_at=timezone.now(),
            )
            Assignment.objects.create(
                judge=membership, project=project, source=AssignmentSource.IMPORT,
                position=membership.assignments.count(),
            )
            for key, value in (raw.get("criteria") or {}).items():
                item = ScoreItem(score=score, criterion=criteria[key], value=value)
                item.full_clean()
                item.save()
            self.remember("score", external_id, score)
            self.report.count("created", "score")


def import_file(path=None):
    return Importer(load(path or settings.FIXTURES_PATH)).run()
