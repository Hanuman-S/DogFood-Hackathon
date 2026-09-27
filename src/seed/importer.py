"""Importing the organizers' shared fixture file.

**This module is the import path, and it deliberately bypasses the deadline guard.**

The fixture describes a hackathon that closed on 2026-03-01. Every project in it was submitted
before that instant, which means no participant write path could ever create these rows -- and
it should not be able to. So the importer writes to the ORM directly and never calls a
participant service function. Two rules keep that bypass honest:

1. Nothing in this module is reachable from any URL. It is imported only by
   `seed/management/commands/import_fixtures.py`, a management command.
2. Every function here is named `import_*` or is private, so a future call site that wanted to
   reuse one for a live participant write would have to do so obviously.

**Idempotent, and deliberately not authoritative.** Rows are keyed on their fixture id
(`external_id`) or a genuine natural key, and `seed.upsert` defaults to **create-only**: missing
rows are inserted, existing rows are never modified. A second run reports every row `unchanged`
and touches nothing.

That default matters more than plain idempotency. The entrypoint runs this on *every* boot, so an
importer that wrote fixture values back whenever they differed would revert real edits: an
organizer extends `submissions_close_at` (which the brief explicitly permits), someone restarts
the container, and the deadline silently snaps back to 2026-03-01. A row that has drifted is
therefore reported as `preserved`, with the names of the differing fields, and left alone.
`import_fixtures --sync` opts into overwriting, and only a human runs that.

**What is synthesized, and what is not.** The fixture gives one date for the whole event
(`submissions_close`) and no descriptions, images, tags or live URLs. Rather than invent
content, the importer leaves absent fields empty and reports the few values it had to derive.
The import report prints both lists, so nobody has to read this file to find out which parts of
the portal's data are the organizers' and which are ours.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass, field
from pathlib import Path

from django.db import transaction

from accounts.models import User
from core import clock
from events.models import (
    CustomQuestion,  # noqa: F401  (imported for symmetry; questions are seeded, not imported)
    Event,
    EventMembership,
    JudgeTrack,
    Prize,
    Role,
    Track,
)
from projects.models import Project, ProjectStatus
from projects.search import rebuild_search_vector
from scoring.models import Criterion, Score, ScoreItem
from seed.upsert import Tally, upsert
from teams.models import Team, TeamMember

# --- synthesis rules -------------------------------------------------------------------
#
# The fixture supplies only `submissions_close`. These two offsets produce the rest of the
# timeline. 72 hours is the length of the DOGFOOD hackathon itself, which makes the synthesized
# window a plausible reading of the organizers' intent rather than an arbitrary guess; 10 days
# matches their published judging window.
SUBMISSION_WINDOW = dt.timedelta(hours=72)
JUDGING_WINDOW = dt.timedelta(days=10)

# The fixture's three criteria keys, with the labels and range the brief specifies.
CRITERION_LABELS = {
    "functionality": "Functionality",
    "quality": "Quality",
    "innovation": "Innovation",
}
CRITERION_MIN = 1
CRITERION_MAX = 5


@dataclass
class ImportReport:
    """What the import did, in a form a human can read at boot time."""

    source: str = ""
    sync: bool = False
    tallies: dict[str, Tally] = field(default_factory=dict)
    synthesized: list[str] = field(default_factory=list)
    absent: list[str] = field(default_factory=list)
    duplicates: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def tally(self, name: str) -> Tally:
        return self.tallies.setdefault(name, Tally())

    @property
    def preserved_total(self) -> int:
        return sum(tally.preserved for tally in self.tallies.values())

    def render(self) -> str:
        lines = [
            "===== FIXTURE IMPORT REPORT =====",
            f"source: {self.source}",
            f"mode:   {'SYNC (existing rows overwritten from the fixture)' if self.sync else 'create-only (existing rows never modified)'}",
            "",
            "entities:",
        ]
        width = max((len(n) for n in self.tallies), default=0)
        for name, tally in self.tallies.items():
            lines.append(f"  {name.ljust(width)}  {tally.summary()}")

        if self.duplicates:
            lines += ["", "duplicate submissions flagged (both rows kept):"]
            lines += [f"  {entry}" for entry in self.duplicates]

        if self.synthesized:
            lines += ["", "synthesized (NOT from the fixture):"]
            lines += [f"  {entry}" for entry in self.synthesized]

        if self.absent:
            lines += ["", "absent from the fixture, left empty (no content invented):"]
            lines += [f"  {entry}" for entry in self.absent]

        divergences = [
            f"  {name}: {entry}"
            for name, tally in self.tallies.items()
            for entry in tally.divergences
        ]
        if divergences:
            lines += [
                "",
                "PRESERVED -- these rows exist and differ from the fixture, and were left "
                "exactly as they are:",
            ]
            lines += divergences
            lines += [
                "  (this is deliberate: a boot-time import must not revert a deliberate edit, "
                "such as an organizer",
                "   extending a deadline. Run `manage.py import_fixtures --sync` to overwrite "
                "them from the fixture.)",
            ]

        skipped = [
            f"  {name}: {reason}"
            for name, tally in self.tallies.items()
            for reason in tally.skipped
        ]
        if skipped:
            lines += ["", "skipped:"]
            lines += skipped

        if self.notes:
            lines += ["", "notes:"]
            lines += [f"  {entry}" for entry in self.notes]

        lines.append("=================================")
        return "\n".join(lines)


class FixtureImporter:
    """Loads `fixtures.json` into the portal's schema, idempotently.

    Args:
        path: the fixture file.
        sync: overwrite existing rows that differ from the fixture. **False by default** -- see
            the module docstring for why a boot-time job must not revert deliberate edits. Only
            `import_fixtures --sync` sets it.
    """

    def __init__(self, path: str | Path, *, sync: bool = False):
        self.path = Path(path)
        self.sync = sync
        self.report = ImportReport(source=str(self.path), sync=sync)
        self.data: dict = {}
        # Fixture id -> ORM object, populated as we go and used to resolve references. Built
        # from the fixture's own ids so that nothing is ever matched by name (the fixture has
        # three teams called "StillTrail").
        self.tracks: dict[str, Track] = {}
        self.teams: dict[str, Team] = {}
        self.projects: dict[str, Project] = {}
        self.judge_memberships: dict[str, EventMembership] = {}
        self.criteria: dict[str, Criterion] = {}
        self.event: Event | None = None

    # -- entry point --------------------------------------------------------------------

    def run(self) -> ImportReport:
        self.data = json.loads(self.path.read_text(encoding="utf-8"))

        # One transaction for the whole import. A half-imported event -- tracks but no
        # projects, projects but no scores -- is worse than no import, because the portal would
        # look seeded while quietly missing rows.
        with transaction.atomic():
            self._import_event()
            self._import_tracks()
            self._import_judges()
            self._import_teams_and_members()
            self._import_projects()
            self._import_criteria_and_scores()
            self._seed_demo_prizes()

        return self.report

    # -- event --------------------------------------------------------------------------

    def _import_event(self) -> None:
        raw = self.data["event"]
        close_at = clock.parse_utc(raw["submissions_close"])

        opens_at = close_at - SUBMISSION_WINDOW
        judging_ends_at = close_at + JUDGING_WINDOW

        slug = self._event_slug(raw)

        self.event, outcome = upsert(
            Event,
            lookup={"external_id": raw["id"]},
            fields={
                "name": raw["name"],
                "slug": slug,
                "submissions_close_at": close_at,
                # starts_at and submissions_open_at are the same instant: the fixture gives no
                # separate start, and inventing a gap would imply information we do not have.
                "starts_at": opens_at,
                "submissions_open_at": opens_at,
                "judging_ends_at": judging_ends_at,
                "gallery_public": True,
            },
            sync=self.sync,
            # The event is the row most likely to have been edited deliberately: the brief
            # explicitly lets an organizer extend `submissions_close_at`. Reporting the
            # divergence by name means an operator sees "submissions_close_at" in the boot log
            # rather than wondering why the deadline they set is still there.
            tally=self.report.tally("event"),
        )
        self.report.tally("event").record(outcome)

        self.report.synthesized += [
            f"event.starts_at         = {clock.iso(opens_at)} (close - 72h)",
            f"event.submissions_open_at = {clock.iso(opens_at)} (close - 72h)",
            f"event.judging_ends_at   = {clock.iso(judging_ends_at)} (close + 10 days)",
            f"event.slug              = {slug} (derived from the name)",
        ]
        self.report.absent.append("event.description (fixture has none)")
        self.report.notes.append(
            f"fixture supplied only submissions_close = {raw['submissions_close']}; "
            "every other date above was derived."
        )

    def _event_slug(self, raw: dict) -> str:
        """Keep an existing event's slug stable across re-imports.

        Recomputing it would either produce the same value or, if a slug collision had been
        resolved with a `-2` suffix, silently change a URL that people have already shared.
        """
        existing = Event.objects.filter(external_id=raw["id"]).first()
        if existing:
            return existing.slug
        return Event.build_slug(raw["name"])

    # -- tracks -------------------------------------------------------------------------

    def _import_tracks(self) -> None:
        tally = self.report.tally("tracks")
        for order, raw in enumerate(self.data.get("tracks", [])):
            track, outcome = upsert(
                Track,
                lookup={"external_id": raw["id"]},
                fields={
                    "event": self.event,
                    "name": raw["name"],
                    "order": order,
                    # Left empty on purpose: the fixture has no track descriptions.
                },
                sync=self.sync,
            )
            self.tracks[raw["id"]] = track
            tally.record(outcome)
        self.report.absent.append("track.description for all 8 tracks (fixture has none)")

    # -- judges -------------------------------------------------------------------------

    def _import_judges(self) -> None:
        users = self.report.tally("judge users")
        memberships = self.report.tally("judge memberships")
        assignments = self.report.tally("judge track assignments")

        for raw in self.data.get("judges", []):
            user, outcome = upsert(
                User,
                lookup={"external_id": raw["id"]},
                fields={
                    "email": raw["email"].strip().lower(),
                    "display_name": raw["name"],
                },
                # `password` is excluded from validation because imported accounts have an
                # unusable password, and from the field list entirely so that a later
                # `seed_demo` run can set one without this import reverting it.
                validate_exclude=("password", "last_login"),
                sync=self.sync,
            )
            if outcome == "created":
                user.set_unusable_password()
                user.save(update_fields=["password"])
            users.record(outcome)

            membership, outcome = upsert(
                EventMembership,
                lookup={"user": user, "event": self.event, "role": Role.JUDGE},
                fields={},
                sync=self.sync,
            )
            self.judge_memberships[raw["id"]] = membership
            memberships.record(outcome)

            for track_id in raw.get("tracks", []):
                track = self.tracks.get(track_id)
                if track is None:
                    assignments.skip(f"{raw['id']} references unknown track {track_id}")
                    continue
                _, outcome = upsert(
                    JudgeTrack,
                    lookup={"membership": membership, "track": track},
                    fields={},
                    sync=self.sync,
                )
                assignments.record(outcome)

        self.report.notes.append(
            "imported judges have an unusable password; DEMO_MODE=1 gives them a known one "
            "via seed_demo, DEMO_MODE=0 leaves them unable to log in."
        )

    # -- teams and their members --------------------------------------------------------

    def _import_teams_and_members(self) -> None:
        teams = self.report.tally("teams")
        users = self.report.tally("participant users")
        members = self.report.tally("team members")

        for raw in self.data.get("teams", []):
            # Keyed on the fixture id, never the name: StillTrail appears three times,
            # AmberSwitch and OpenSignal twice each. Matching by name would silently merge
            # unrelated teams.
            team, outcome = upsert(
                Team,
                lookup={"external_id": raw["id"]},
                fields={"event": self.event, "name": raw["name"]},
                sync=self.sync,
            )
            self.teams[raw["id"]] = team
            teams.record(outcome)

            for position, email in enumerate(raw.get("members", [])):
                email = email.strip().lower()
                user, user_outcome = self._import_participant(email)
                users.record(user_outcome)

                # First listed member is the captain, per the brief.
                is_captain = position == 0

                _, outcome = upsert(
                    TeamMember,
                    # `(event, user)` is the unique key -- one team per user per event -- so it
                    # is also the right lookup. A participant who somehow appeared under two
                    # teams would be moved rather than duplicated, and the database would
                    # refuse a duplicate anyway.
                    lookup={"event": self.event, "user": user},
                    fields={"team": team, "is_captain": is_captain},
                    sync=self.sync,
                )
                members.record(outcome)

                _, membership_outcome = upsert(
                    EventMembership,
                    lookup={"user": user, "event": self.event, "role": Role.PARTICIPANT},
                    fields={},
                    sync=self.sync,
                )

        self.report.notes.append(
            "participant display names are the email local part: the fixture lists team "
            "members as bare email addresses and supplies no names."
        )

    def _import_participant(self, email: str) -> tuple[User, str]:
        """Create or find a participant account from a bare email address.

        The fixture identifies team members only by email, so there is no `external_id` to key
        on and no name to use. The display name is the local part of the address, verbatim --
        a visible derivation rather than an invented human name.
        """
        user, outcome = upsert(
            User,
            lookup={"email": email},
            fields={"display_name": email.split("@")[0]},
            validate_exclude=("password", "last_login"),
            sync=self.sync,
        )
        if outcome == "created":
            user.set_unusable_password()
            user.save(update_fields=["password"])
        return user, outcome

    # -- projects -----------------------------------------------------------------------

    def _import_projects(self) -> None:
        tally = self.report.tally("projects")

        # Fixture order is submission order for the purpose of duplicate detection: the
        # canonical row is the earlier one, and `prj_41` (17:57Z) follows `prj_07` (04:29Z) in
        # the file. Sorting by `submitted_at` gives the same answer here and is not dependent
        # on file order staying meaningful.
        raw_projects = sorted(
            self.data.get("projects", []),
            key=lambda p: (p.get("submitted_at", ""), p.get("id", "")),
        )

        for raw in raw_projects:
            team = self.teams.get(raw["team"])
            track = self.tracks.get(raw["track"])
            if team is None:
                tally.skip(f"{raw['id']} references unknown team {raw['team']}")
                continue
            if track is None:
                tally.skip(f"{raw['id']} references unknown track {raw['track']}")
                continue

            duplicate_of = self._find_duplicate_target(raw, team)

            project, outcome = upsert(
                Project,
                lookup={"external_id": raw["id"]},
                fields={
                    "event": self.event,
                    "team": team,
                    "track": track,
                    "name": raw["title"],
                    "tagline": raw.get("summary", "")[:140],
                    "repo_url": raw.get("repo_url", ""),
                    "status": ProjectStatus.SUBMITTED,
                    "submitted_at": clock.parse_utc(raw["submitted_at"]),
                    "duplicate_of": duplicate_of,
                    # description, thumbnail, images, video URL, live URL and tags stay empty.
                },
                validate_exclude=("search_vector",),
                sync=self.sync,
            )
            self.projects[raw["id"]] = project
            tally.record(outcome)

            if duplicate_of is not None:
                self.report.duplicates.append(
                    f"{raw['id']} '{raw['title']}' duplicates "
                    f"{duplicate_of.external_id} (same team {team.external_id}, "
                    f"same title and repo_url); {duplicate_of.external_id} is canonical, "
                    "both rows kept with their scores."
                )

            rebuild_search_vector(project)

        self.report.absent += [
            "project.description for all projects (fixture has none)",
            "project.thumbnail and gallery images (fixture has none)",
            "project.demo_video_url and live_url (fixture has none)",
            "project tags (fixture has none)",
        ]
        self.report.notes.append(
            "all 41 fixture summaries are the same sentence, so tagline-weighted search "
            "does not discriminate between fixture projects."
        )

    def _find_duplicate_target(self, raw: dict, team: Team) -> Project | None:
        """Return the earlier project this one duplicates, if any.

        The rule: **same team** and (**same normalized title** or **same repo_url**), where the
        earlier submission is canonical. Scoped to one team on purpose -- two teams choosing the
        same project name is a coincidence, not a duplicate submission.

        Both rows are kept. The later one is flagged, not deleted, because it carries scores of
        its own and deleting it would destroy the evidence an organizer needs to decide what to
        do. T2 decides how to treat the pair.
        """
        title_key = _normalize_title(raw.get("title", ""))
        repo = (raw.get("repo_url") or "").strip().lower()
        submitted_at = clock.parse_utc(raw["submitted_at"])

        candidates = (
            Project.objects.filter(team=team, duplicate_of__isnull=True)
            .exclude(external_id=raw["id"])
            .order_by("submitted_at")
        )
        for candidate in candidates:
            if candidate.submitted_at and candidate.submitted_at > submitted_at:
                # A later row is never the canonical one.
                continue
            same_title = _normalize_title(candidate.name) == title_key
            same_repo = bool(repo) and candidate.repo_url.strip().lower() == repo
            if same_title or same_repo:
                return candidate
        return None

    # -- criteria and scores ------------------------------------------------------------

    def _import_criteria_and_scores(self) -> None:
        criteria_tally = self.report.tally("criteria")
        scores_tally = self.report.tally("scores")
        items_tally = self.report.tally("score items")

        raw_scores = self.data.get("scores", [])

        # Criteria are discovered from the data rather than hardcoded, so a fixture that adds a
        # fourth key imports without a code change.
        keys: list[str] = []
        for raw in raw_scores:
            for key in raw.get("criteria", {}):
                if key not in keys:
                    keys.append(key)

        for order, key in enumerate(keys):
            criterion, outcome = upsert(
                Criterion,
                lookup={"event": self.event, "key": key},
                fields={
                    "label": CRITERION_LABELS.get(key, key.replace("_", " ").title()),
                    "weight": 1,
                    "min_value": CRITERION_MIN,
                    "max_value": CRITERION_MAX,
                    "order": order,
                    "external_id": f"{self.event.external_id}:crit:{key}",
                },
                sync=self.sync,
            )
            self.criteria[key] = criterion
            criteria_tally.record(outcome)

        if keys:
            self.report.synthesized.append(
                "criterion weight=1.0, min=1, max=5 for "
                f"{', '.join(keys)} (fixture gives values but no rubric definition)"
            )

        for raw in raw_scores:
            membership = self.judge_memberships.get(raw["judge"])
            project = self.projects.get(raw["project"])
            if membership is None:
                scores_tally.skip(f"score references unknown judge {raw['judge']}")
                continue
            if project is None:
                scores_tally.skip(f"score references unknown project {raw['project']}")
                continue

            score, outcome = upsert(
                Score,
                # (judge, project) is the model's unique key, and the fixture gives scores no
                # id of their own.
                lookup={"judge": membership, "project": project},
                # Empty comments are expected: 51 of the 126 fixture reviews have none.
                fields={"comment": raw.get("comment", "") or ""},
                sync=self.sync,
            )
            scores_tally.record(outcome)

            for key, value in (raw.get("criteria") or {}).items():
                criterion = self.criteria.get(key)
                if criterion is None:
                    items_tally.skip(f"score item references unknown criterion {key}")
                    continue
                _, outcome = upsert(
                    ScoreItem,
                    lookup={"score": score, "criterion": criterion},
                    fields={"value": value},
                    sync=self.sync,
                )
                items_tally.record(outcome)

        self.report.notes.append(
            "review coverage is uneven by design: projects carry 2 to 5 reviews and judges "
            "1 to 11. Nothing in the schema assumes a balanced matrix."
        )

    # -- prizes -------------------------------------------------------------------------

    def _seed_demo_prizes(self) -> None:
        """The fixture has no prizes, so a demo set is created and labelled as such.

        Every description says so in words, because a prize list is exactly the kind of thing a
        reader would otherwise assume came from the organizers.
        """
        tally = self.report.tally("prizes (demo)")
        marker = (
            "Demo data created by import_fixtures: the organizer fixture contains no prizes."
        )

        _, outcome = upsert(
            Prize,
            lookup={"external_id": f"{self.event.external_id}:prize:grand"},
            fields={
                "event": self.event,
                "track": None,
                "name": "Grand Prize",
                "description": marker,
                "value_text": "",
                "order": 0,
            },
            sync=self.sync,
        )
        tally.record(outcome)

        for order, (track_id, track) in enumerate(self.tracks.items(), start=1):
            _, outcome = upsert(
                Prize,
                lookup={"external_id": f"{self.event.external_id}:prize:track:{track_id}"},
                fields={
                    "event": self.event,
                    "track": track,
                    "name": f"Best in {track.name}",
                    "description": marker,
                    "value_text": "",
                    "order": order,
                },
                sync=self.sync,
            )
            tally.record(outcome)

        self.report.synthesized.append(
            f"{tally.total} prizes (1 grand + 1 per track), marked as demo data in their "
            "descriptions; the fixture has none."
        )


def _normalize_title(title: str) -> str:
    """Casefold and collapse whitespace, for duplicate comparison only.

    Never used for display or for matching teams -- only to decide whether two submissions from
    the same team are the same submission typed twice.
    """
    return " ".join((title or "").split()).casefold()
