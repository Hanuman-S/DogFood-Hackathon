"""CSV export: everything an organizer needs out of an event, at any stage.

Routes (sessions and Bearer tokens alike; organizers of the event and platform admins only):

    GET /api/export.csv                        the `projects` sheet for every event the caller
                                               manages, one row per project (the acceptance
                                               checker's route: 200 and a CSV body)
    GET /api/export.csv?event=<slug>&sheet=<name>   one sheet of one event
    GET /api/export.zip?event=<slug>           every sheet of one event, one CSV each, plus a
                                               README.txt saying what each holds

Each sheet belongs to a stage and can be downloaded only once that stage has started (by the
database clock): setup and audit from the moment the event exists, teams / members / projects from
submissions open, assignments from submissions close, reviews from judging start, results from
judging end (rank, score and judge lean stay empty everywhere until then). A sheet of a stage that
has not started is refused (409 `stage_not_open`) and left out of the ZIP. An open sheet with nothing
in it yet is a header row.

Rules:
* **One consistent picture.** Every sheet of a download is read inside one REPEATABLE READ,
  READ ONLY transaction, so a review submitted mid-download cannot appear in `reviews` but not in
  `projects`. The request must not already be in a transaction (ATOMIC_REQUESTS is off).
* **Submitted reviews only.** A draft is the judge's own until they submit it: it is counted
  (`judges.drafts`) but its values are never exported.
* **Results** (after judging ends). The latest *final* snapshot when one exists; otherwise one
  computed by the engine for the export and labelled as such (nothing is saved).
* **Secrets stay out.** Team invite tokens, judge invite digests and password data are never
  exported.
* **Spreadsheet-safe.** A text cell starting with = + - @ or a tab/CR is prefixed with ' so a
  spreadsheet shows it instead of running it as a formula. UTF-8 with a BOM, so Excel reads names
  with accents correctly.
* Every download is audited (`export_downloaded`); refusals too (`access_denied`).
"""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property

from django.db import connection, transaction
from django.db.models import Count, Q
from django.http import HttpResponse
from django.views.decorators.cache import never_cache
from django.views.decorators.http import require_GET

from accounts.roles import Role, is_organizer_of
from core import audit
from core.api import error
from core.deadlines import db_now
from core.models import AuditAction, AuditLog
from events.models import CustomQuestion, Event, EventMembership, JudgeInvite, Prize, Track
from projects.models import Answer, Project
from scoring import services as scoring
from scoring.models import (Assignment, AssignmentRound, AssignmentStatus, Criterion, Publication,
                            ResultSnapshot, Score, SnapshotKind)
from teams.models import Team, TeamExtension, TeamMember

# --- cell formatting ------------------------------------------------------------------------------

FORMULA_START = ("=", "+", "-", "@", "\t", "\r")


def cell(value):
    """A value as it should appear in a spreadsheet cell."""
    if value is None:
        return ""
    if isinstance(value, bool):
        return "yes" if value else "no"
    if hasattr(value, "isoformat") and hasattr(value, "tzinfo"):
        return value.isoformat().replace("+00:00", "Z")
    if isinstance(value, int):
        return str(value)  # a number, even a negative one, is never a formula
    if isinstance(value, Decimal):
        return format(value.normalize(), "f")
    if isinstance(value, float):
        return f"{value:.6f}".rstrip("0").rstrip(".") if value == value else ""
    if isinstance(value, (list, tuple)):
        value = "; ".join(str(v) for v in value)
    if isinstance(value, dict):
        value = json.dumps(value, sort_keys=True, ensure_ascii=False)
    text = str(value)
    if text.startswith(FORMULA_START):
        return "'" + text
    return text


def to_csv(header, rows) -> bytes:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")
    writer.writerow([cell(h) for h in header])
    for row in rows:
        writer.writerow([cell(v) for v in row])
    return ("﻿" + buffer.getvalue()).encode("utf-8")


# --- the sheets -----------------------------------------------------------------------------------

@dataclass(frozen=True)
class Sheet:
    name: str
    stage: str
    about: str
    # The Event field at which this sheet's stage starts; None = from the moment the event exists.
    opens: str | None = None

    def opens_at(self, event):
        return getattr(event, self.opens) if self.opens else None

    def is_open(self, event, now):
        opens = self.opens_at(event)
        return opens is None or now >= opens


# When each stage starts. Assignment runs in the gap between the close and judging (the "closed"
# phase), so its sheets open at the close.
SUBMISSIONS, ASSIGNMENT, JUDGING, RESULTS = (
    "submissions_open_at", "submissions_close_at", "judging_starts_at", "judging_ends_at")


SHEETS = [
    Sheet("event", "setup", "the event's settings and timeline (original dates kept when extended), organizers, and headline counts"),
    Sheet("tracks", "setup", "tracks, with the number of submitted projects and judges in each"),
    Sheet("prizes", "setup", "prizes and the track each belongs to"),
    Sheet("questions", "setup", "custom submission questions and how many projects answered each"),
    Sheet("rubric", "setup", "criteria: weight, share of the score, scale and the written description of each level"),
    Sheet("judges", "setup", "judges: tracks and progress (assigned, submitted, drafts, declined); their lean once results open"),
    Sheet("judge_invites", "setup", "judge invite links: who for, state, and who accepted (the link itself is never exported)"),
    Sheet("teams", "submissions", "teams: captain, members, project and any deadline extension", SUBMISSIONS),
    Sheet("members", "submissions", "one row per participant: team, name, email, captain or not", SUBMISSIONS),
    Sheet("projects", "submissions", "one row per project: status, links, tags, custom answers, reviews in; rank and score once results open", SUBMISSIONS),
    Sheet("assignments", "assignment", "every assignment ever made, including declined (with the reason) and withdrawn ones", ASSIGNMENT),
    Sheet("assignment_rounds", "assignment", "each automatic assignment round, with the seed that reproduces it", ASSIGNMENT),
    Sheet("reviews", "judging", "every submitted review: one column per criterion, the judge's weighted rating and comment (drafts are never exported)", JUDGING),
    Sheet("results", "results", "the ranking: the final snapshot if computed, otherwise one computed for the export; every method's rank and score side by side", RESULTS),
    Sheet("audit", "audit", "the event's audit trail: who did what and when"),
]
SHEET_NAMES = [s.name for s in SHEETS]
SHEETS_BY_NAME = {s.name: s for s in SHEETS}


def sheet_states(event, now):
    """[(sheet, open?, opens at)] for the event page."""
    return [(s, s.is_open(event, now), s.opens_at(event)) for s in SHEETS]


class EventExport:
    """Builds the sheets of one event. Construct and use inside `consistent_read()`."""

    def __init__(self, event, *, now=None):
        self.event = event
        self.now = now or db_now()

    # --- shared lookups --------------------------------------------------------------------------

    @cached_property
    def criteria(self):
        return list(Criterion.objects.filter(event=self.event).order_by("order", "key"))

    @cached_property
    def judges(self):
        return list(
            EventMembership.objects.filter(event=self.event, role=Role.JUDGE)
            .select_related("user", "added_by").prefetch_related("judge_tracks__track").order_by("user__name", "pk")
        )

    @cached_property
    def projects(self):
        return list(
            Project.objects.filter(event=self.event)
            .select_related("team", "track", "last_edited_by").prefetch_related("tags", "images")
            .order_by("name", "pk")
        )

    @cached_property
    def target(self):
        return scoring.review_target(self.event)

    @cached_property
    def scores(self):
        """(judge_id, project_id) -> (submitted?, updated_at), for every review, drafts included."""
        return {
            (j, p): (s is not None, u)
            for j, p, s, u in Score.objects.filter(project__event=self.event)
            .values_list("judge_id", "project_id", "submitted_at", "updated_at")
        }

    @cached_property
    def live_assignments(self):
        return list(Assignment.objects.filter(project__event=self.event, status=AssignmentStatus.ASSIGNED)
                    .values_list("judge_id", "project_id"))

    @cached_property
    def results(self):
        """(source text, comparison dict or None). Never writes anything. Nothing before judging
        ends: results are a stage of their own, so rank, score and lean stay empty until then."""
        if not SHEETS_BY_NAME["results"].is_open(self.event, self.now):
            return f"results open when judging ends ({cell(self.event.judging_ends_at)})", None
        final = (ResultSnapshot.objects.filter(event=self.event, kind=SnapshotKind.FINAL)
                 .order_by("-created_at", "-id").first())
        if final is not None:
            published = Publication.objects.filter(event=self.event, snapshot=final, unpublished_at__isnull=True).exists()
            source = f"final snapshot #{final.pk} computed {cell(final.created_at)}" + (" (published)" if published else "")
            return source, final.comparison
        if not self.criteria:
            return "no rubric yet", None
        try:
            inp = scoring.build_input(self.event)
            if not inp.reviews:
                return "no submitted reviews yet", None
            cfg = scoring.event_config(self.event)
            comparison = scoring.pipeline.compare(inp, [cfg.primary, *cfg.compare], config=cfg)
        except Exception as problem:  # the engine refusing the input is information, not a crash
            return f"results could not be computed: {problem}", None
        return "computed for this export, no final snapshot yet (not saved)", scoring.to_jsonable(comparison)

    @cached_property
    def primary_result(self):
        _, comparison = self.results
        if not comparison:
            return None
        return comparison["results"][comparison["primary"]]

    @cached_property
    def result_by_project(self):
        result = self.primary_result
        return {row["project_id"]: row for row in result["projects"]} if result else {}

    @cached_property
    def result_by_judge(self):
        result = self.primary_result
        return {row["judge_id"]: row for row in result["judges"]} if result else {}

    # --- setup ------------------------------------------------------------------------------------

    def sheet_event(self):
        e = self.event
        organizers = EventMembership.objects.filter(event=e, role=Role.ORGANIZER).select_related("user")
        members = TeamMember.objects.filter(event=e)
        submitted = sum(1 for p in self.projects if p.is_submitted)
        reviews_in = sum(1 for done, _ in self.scores.values() if done)
        rows = [
            ("name", e.name), ("slug", e.slug), ("tagline", e.tagline), ("published", e.is_published),
            ("stage now", e.phase_at(self.now).label.lower()),
            ("event starts", e.starts_at), ("submissions open", e.submissions_open_at),
            ("submissions close", e.submissions_close_at),
            ("submissions close, originally", e.original_submissions_close_at),
            ("judging starts", e.judging_starts_at), ("judging ends", e.judging_ends_at),
            ("judging ends, originally", e.original_judging_ends_at),
            ("results", e.results_at or "to be announced"),
            ("team size", f"{e.min_team_size}-{e.max_team_size}"),
            ("reviews per project (target)", self.target),
            ("organizers", [f"{m.user.name} <{m.user.email}>" for m in organizers]),
            ("created by", e.created_by.email if e.created_by else ""), ("created", e.created_at),
            ("teams", Team.objects.filter(event=e).count()), ("participants", members.count()),
            ("projects submitted", submitted), ("projects in draft", len(self.projects) - submitted),
            ("judges", len(self.judges)), ("reviews submitted", reviews_in),
            ("results source", self.results[0]),
            ("exported at", self.now),
        ]
        return ["field", "value"], rows

    def sheet_tracks(self):
        tracks = Track.objects.filter(event=self.event).order_by("order", "name")
        submitted = Counter(p.track_id for p in self.projects if p.is_submitted)
        judges = Counter()
        for j in self.judges:
            covered = {jt.track_id for jt in j.judge_tracks.all()}
            for t in tracks:
                if not covered or t.pk in covered:
                    judges[t.pk] += 1
        return (["order", "track", "description", "hidden", "projects submitted", "judges who cover it"],
                [(t.order, t.name, t.description, t.is_hidden, submitted[t.pk], judges[t.pk]) for t in tracks])

    def sheet_prizes(self):
        prizes = Prize.objects.filter(event=self.event).select_related("track").order_by("rank", "title")
        return (["rank", "prize", "value", "track", "description"],
                [(p.rank, p.title, p.value, p.track.name if p.track else "whole event", p.description) for p in prizes])

    def sheet_questions(self):
        questions = (CustomQuestion.objects.filter(event=self.event).order_by("order", "pk")
                     .annotate(n=Count("answers", filter=~Q(answers__value=""))))
        return (["order", "question", "hint", "kind", "choices", "required", "hidden", "projects that answered"],
                [(q.order, q.prompt, q.help_text, q.get_kind_display().lower(), q.choices, q.required,
                  q.is_hidden, q.n) for q in questions])

    def sheet_rubric(self):
        shares = scoring.weight_shares(self.criteria)
        header = ["order", "key", "criterion", "weight", "share of score (%)", "lowest", "highest", "description"]
        header += [f"level {n}" for n in range(1, 6)]
        rows = []
        for c in self.criteria:
            levels = [c.level_descriptions.get(str(n), "") for n in range(1, 6)]
            rows.append((c.order, c.key, c.label, c.weight.normalize(), shares.get(c.pk), c.min_value,
                         c.max_value, c.description, *levels))
        return header, rows

    # --- people -----------------------------------------------------------------------------------

    def sheet_judges(self):
        assigned = defaultdict(set)
        for judge_id, project_id in self.live_assignments:
            assigned[judge_id].add(project_id)
        declined = Counter(Assignment.objects.filter(project__event=self.event, status=AssignmentStatus.DECLINED)
                           .values_list("judge_id", flat=True))
        header = ["judge", "email", "tracks", "added", "added by", "assigned", "submitted", "drafts",
                  "not started", "declined (conflict)", "last active", "lean (from results)", "result flags"]
        rows = []
        for j in self.judges:
            mine = assigned[j.pk]
            state = {p: self.scores.get((j.pk, p)) for p in mine}
            done = sum(1 for s in state.values() if s and s[0])
            drafts = sum(1 for s in state.values() if s and not s[0])
            active = [u for (judge, _), (_, u) in self.scores.items() if judge == j.pk and u]
            lean = self.result_by_judge.get(str(j.pk), {})
            rows.append((
                j.user.name, j.user.email, [jt.track.name for jt in j.judge_tracks.all()] or "all tracks",
                j.added_at, j.added_by.email if j.added_by else "", len(mine), done, drafts,
                len(mine) - done - drafts, declined[j.pk], max(active) if active else "never",
                lean.get("bias_centred"), lean.get("flags"),
            ))
        return header, rows

    def sheet_judge_invites(self):
        invites = (JudgeInvite.objects.filter(event=self.event).select_related("created_by", "accepted_by")
                   .prefetch_related("tracks").order_by("-created_at"))

        def state(i):
            if i.accepted_at:
                return "accepted"
            if i.revoked_at:
                return "revoked"
            if i.expires_at <= self.now:
                return "expired"
            return "pending"

        return (["for", "tracks", "state", "created", "created by", "expires", "accepted", "accepted by", "revoked"],
                [(i.email or "open link (anyone with it)", [t.name for t in i.tracks.all()] or "all tracks",
                  state(i), i.created_at, i.created_by.email if i.created_by else "", i.expires_at,
                  i.accepted_at, i.accepted_by.email if i.accepted_by else "", i.revoked_at) for i in invites])

    # --- submissions ------------------------------------------------------------------------------

    def sheet_teams(self):
        teams = (Team.objects.filter(event=self.event).select_related("captain")
                 .prefetch_related("members__user").order_by("name"))
        extensions = {x.team_id: x for x in TeamExtension.objects.filter(team__event=self.event).select_related("granted_by")}
        project_of = {p.team_id: p for p in self.projects}
        header = ["team id", "team", "captain", "captain email", "members", "member names", "created",
                  "project", "project status", "extended until", "extension reason", "extension granted by"]
        rows = []
        for t in teams:
            x, p = extensions.get(t.pk), project_of.get(t.pk)
            rows.append((t.pk, t.name, t.captain.name, t.captain.email, len(t.members.all()),
                         [m.user.name for m in t.members.all()], t.created_at,
                         p.name if p else "", p.status if p else "no project",
                         x.until if x else "", x.reason if x else "", x.granted_by.email if x and x.granted_by else ""))
        return header, rows

    def sheet_members(self):
        members = (TeamMember.objects.filter(event=self.event).select_related("team", "user")
                   .order_by("team__name", "user__name"))
        return (["team", "name", "email", "captain", "joined"],
                [(m.team.name, m.user.name, m.user.email, m.team.captain_id == m.user_id, m.joined_at) for m in members])

    @cached_property
    def reviewers_per_project(self):
        return Counter(project for _, project in self.live_assignments)

    @cached_property
    def reviews_in_per_project(self):
        return Counter(project for (_, project), (done, _) in self.scores.items() if done)

    def _project_columns(self, p):
        reviewers = self.reviewers_per_project[p.pk]
        reviews_in = self.reviews_in_per_project[p.pk]
        result = self.result_by_project.get(str(p.pk), {})
        return [
            p.pk, p.name, p.team.name, p.track.name if p.track else "", p.status, p.submitted_at,
            p.tagline, p.repo_url, p.demo_video_url, p.live_url, [t.name for t in p.tags.all()],
            len(p.images.all()), p.updated_at, reviewers, reviews_in, self.target,
            result.get("rank"), result.get("score"), result.get("se"), result.get("flags"),
        ]

    PROJECT_HEADER = ["project id", "project", "team", "track", "status", "submitted", "tagline", "repo",
                      "demo video", "live url", "tags", "images", "last edited", "judges assigned",
                      "reviews submitted", "reviews wanted", "rank", "score", "standard error", "result flags"]

    def sheet_projects(self):
        questions = list(CustomQuestion.objects.filter(event=self.event).order_by("order", "pk"))
        answers = defaultdict(dict)
        for project_id, question_id, value in Answer.objects.filter(project__event=self.event).values_list(
                "project_id", "question_id", "value"):
            answers[project_id][question_id] = value
        header = self.PROJECT_HEADER + [f"answer: {q.prompt}" for q in questions]
        rows = [self._project_columns(p) + [answers[p.pk].get(q.pk, "") for q in questions] for p in self.projects]
        return header, rows

    # --- judging ----------------------------------------------------------------------------------

    def sheet_assignments(self):
        assignments = (Assignment.objects.filter(project__event=self.event)
                       .select_related("judge__user", "project", "created_by").order_by("project__name", "created_at", "pk"))

        def review(a):
            s = self.scores.get((a.judge_id, a.project_id))
            return "not started" if s is None else ("submitted" if s[0] else "draft")

        return (["project id", "project", "judge", "judge email", "status", "review", "source", "round",
                 "queue position", "decline reason", "created", "created by", "status changed"],
                [(a.project_id, a.project.name, a.judge.user.name, a.judge.user.email,
                  a.get_status_display().lower(), review(a), a.get_source_display().lower(), a.round_id,
                  a.position, a.decline_reason, a.created_at, a.created_by.email if a.created_by else "",
                  a.status_changed_at) for a in assignments])

    def sheet_assignment_rounds(self):
        rounds = AssignmentRound.objects.filter(event=self.event).select_related("created_by").order_by("pk")
        return (["round", "kind", "seed", "reviews per project", "max per judge", "added", "still missing",
                 "by", "at"],
                [(r.pk, r.get_kind_display().lower(), r.seed, r.target_reviews, r.max_load,
                  (r.summary or {}).get("added"), len((r.summary or {}).get("short") or []),
                  r.created_by.email if r.created_by else "", r.created_at) for r in rounds])

    def sheet_reviews(self):
        scores = (Score.objects.filter(project__event=self.event, submitted_at__isnull=False)
                  .select_related("judge__user", "project__team", "project__track")
                  .prefetch_related("items").order_by("project__name", "judge__user__name"))
        header = ["project id", "project", "team", "track", "judge", "judge email"]
        header += [f"{c.label} ({c.key}, {c.min_value}-{c.max_value})" for c in self.criteria]
        header += ["weighted rating", "comment", "submitted", "last changed"]
        rows = []
        for s in scores:
            items = {i.criterion_id: i.value for i in s.items.all()}
            rating = scoring.weighted_rating(self.criteria, items)
            rows.append([s.project_id, s.project.name, s.project.team.name,
                         s.project.track.name if s.project.track else "", s.judge.user.name, s.judge.user.email,
                         *[items.get(c.pk) for c in self.criteria],
                         round(float(rating), 4) if rating is not None else None,
                         s.comment, s.submitted_at, s.updated_at])
        return header, rows

    # --- results ----------------------------------------------------------------------------------

    def sheet_results(self):
        source, comparison = self.results
        header = ["source", "method", "rank", "tie group", "project", "team", "track", "score", "standard error",
                  "reviews", "flags"]
        if not comparison:
            return header, [(source,) + ("",) * (len(header) - 1)]
        methods = [m for m in comparison["methods"] if m != comparison["primary"]]
        header += [f"{m}: {what}" for m in methods for what in ("rank", "score")]
        header += [f"places gained vs {comparison['baseline']}"] if comparison.get("baseline") != comparison["primary"] else []
        by_id = {str(p.pk): p for p in self.projects}
        tracks = {str(t.pk): t.name for t in Track.objects.filter(event=self.event)}
        other = {row["project_id"]: row for row in comparison.get("rows", [])}
        rows = []
        for r in sorted(self.primary_result["projects"], key=lambda r: (r["rank"], r["project_id"])):
            project = by_id.get(r["project_id"])
            name = project.name if project else f"{r['project_id']} (folded duplicate)"
            row = [source, comparison["primary"], r["rank"], r["tie_group"], name, project.team.name if project else "",
                   tracks.get(r["track_id"] or "", ""), r["score"], r["se"], r["n_reviews"], r["flags"]]
            cmp_row = other.get(r["project_id"], {})
            for m in methods:
                row += [cmp_row.get("ranks", {}).get(m), cmp_row.get("scores", {}).get(m)]
            if comparison.get("baseline") != comparison["primary"]:
                row.append(cmp_row.get("change_vs_baseline", {}).get(comparison["primary"]))
            rows.append(row)
        return header, rows

    # --- audit ------------------------------------------------------------------------------------

    def sheet_audit(self):
        # Only rows tied to this event by slug: matching by team or project name could pull in
        # another event's rows (names are unique per event, not across events).
        slug = self.event.slug
        entries = AuditLog.objects.filter(Q(subject=slug) | Q(detail__event=slug)).order_by("created_at", "pk")
        return (["at", "who", "action", "action code", "subject", "detail", "ip"],
                [(e.created_at, e.actor_email, e.get_action_display(), e.action, e.subject, e.detail, e.ip)
                 for e in entries])

    # --- output -----------------------------------------------------------------------------------

    def sheet(self, name):
        return getattr(self, f"sheet_{name}")()

    def zip_bytes(self):
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
            for n, s in enumerate(SHEETS, start=1):
                if not s.is_open(self.event, self.now):
                    continue  # that stage has not started: not in the download at all
                header, rows = self.sheet(s.name)
                archive.writestr(f"{n:02d}-{s.stage}-{s.name}.csv", to_csv(header, rows))
            archive.writestr("README.txt", self.readme())
        return buffer.getvalue()

    def readme(self):
        lines = [
            f"DOGFOOD export of {self.event.name} ({self.event.slug})",
            f"exported at {cell(self.now)}, stage: {self.event.phase_at(self.now).label.lower()}",
            "",
            "Every file was read at the same instant (one consistent database snapshot).",
            "Times are UTC (ISO 8601). A file with only a header row has nothing in it yet.",
            "Only submitted reviews are exported; drafts are counted in the judges file, never shown.",
            f"Results: {self.results[0]}.",
            "",
        ]
        waiting = []
        for n, s in enumerate(SHEETS, start=1):
            if s.is_open(self.event, self.now):
                lines.append(f"{n:02d}-{s.stage}-{s.name}.csv  {s.about}")
            else:
                waiting.append(f"{s.name} (opens {cell(s.opens_at(self.event))})")
        if waiting:
            lines += ["", "Not in this download, because their stage has not started yet:", *[f"  {w}" for w in waiting]]
        return "\n".join(lines) + "\n"


class ExportInsideTransaction(RuntimeError):
    pass


def consistent_read():
    """A read-only transaction in which every query sees the same snapshot of the database.

    Raises inside another transaction, like `scoring.services.compute_snapshot`: only the
    outermost transaction can choose its isolation level, so a nested one would silently lose the
    one-instant guarantee. Views use @transaction.non_atomic_requests; tests use
    django_db(transaction=True)."""
    class _Snapshot:
        def __enter__(self):
            if connection.in_atomic_block:
                raise ExportInsideTransaction(
                    "the export opens its own REPEATABLE READ transaction and cannot run inside "
                    "another. Call it outside transaction.atomic (views: @transaction.non_atomic_requests)."
                )
            self.atomic = transaction.atomic()
            self.atomic.__enter__()
            if connection.vendor == "postgresql":
                with connection.cursor() as cursor:
                    cursor.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ, READ ONLY")
            return self

        def __exit__(self, *exc):
            return self.atomic.__exit__(*exc)

    return _Snapshot()


# --- views ----------------------------------------------------------------------------------------

def _refuse(request, status, code, detail, **extra):
    if status in (401, 403):
        audit.record(AuditAction.ACCESS_DENIED, request=request, subject="api:export", reason=code)
    response = error(status, code, detail, **extra)
    if status == 401:
        response["WWW-Authenticate"] = "Bearer"
    return response


def _caller_events(request):
    """(events, refusal response or None) for a request that may name ?event=<slug>."""
    user = request.user
    if not user.is_authenticated:
        return None, _refuse(request, 401, "unauthenticated", "Log in or send a Bearer token.")
    managed = Event.objects.managed_by(user)
    if not managed.exists():
        return None, _refuse(request, 403, "forbidden", "Only organizers can export event data.")
    slug = request.GET.get("event")
    if slug:
        event = Event.objects.filter(slug=slug).first()
        if event is None or not is_organizer_of(user, event):
            return None, error(404, "not_found", "No such event.")
        return [event], None
    return list(managed.order_by("-submissions_close_at", "slug")), None


def _stamp(now):
    return now.strftime("%Y%m%d-%H%MZ")


def _download(body, content_type, filename):
    response = HttpResponse(body, content_type=content_type)
    response["Content-Disposition"] = f'attachment; filename="{filename}"'
    response["X-Content-Type-Options"] = "nosniff"
    return response


@never_cache
@require_GET
@transaction.non_atomic_requests
def export_csv(request):
    events, refusal = _caller_events(request)
    if refusal:
        return refusal
    sheet = request.GET.get("sheet") or "projects"
    if sheet not in SHEET_NAMES:
        return error(400, "unknown_sheet", f"sheet must be one of: {', '.join(SHEET_NAMES)}.", sheets=SHEET_NAMES)
    single = bool(request.GET.get("event"))
    if not single and sheet != "projects":
        return error(400, "event_required",
                     "Across all your events only the projects sheet is available; add ?event=<slug> for the others.")
    with consistent_read():
        now = db_now()
        if single:
            event = events[0]
            if not SHEETS_BY_NAME[sheet].is_open(event, now):
                opens = SHEETS_BY_NAME[sheet].opens_at(event)
                return error(409, "stage_not_open",
                             f"The {sheet} sheet opens with its stage, at {cell(opens)}.",
                             opens_at=cell(opens), stage=SHEETS_BY_NAME[sheet].stage)
            header, rows = EventExport(event, now=now).sheet(sheet)
        else:
            # Only events whose submissions have opened: the others have no projects stage yet.
            header, rows = ["event"] + EventExport.PROJECT_HEADER, []
            for event in events:
                if not SHEETS_BY_NAME["projects"].is_open(event, now):
                    continue
                export = EventExport(event, now=now)
                rows += [[event.slug] + export._project_columns(p) for p in export.projects]
    audit.record(AuditAction.EXPORT_DOWNLOADED, request=request,
                 subject=events[0].slug if single else "all managed events",
                 event=events[0].slug if single else "", format="csv", sheet=sheet, rows=len(rows))
    name = f"{events[0].slug if single else 'dogfood-all-events'}-{sheet}-{_stamp(now)}.csv"
    return _download(to_csv(header, rows), "text/csv; charset=utf-8", name)


@never_cache
@require_GET
@transaction.non_atomic_requests
def export_zip(request):
    if not request.GET.get("event"):
        if not request.user.is_authenticated:
            return _refuse(request, 401, "unauthenticated", "Log in or send a Bearer token.")
        return error(400, "event_required", "Add ?event=<slug>: the full export is per event.")
    events, refusal = _caller_events(request)
    if refusal:
        return refusal
    event = events[0]
    with consistent_read():
        export = EventExport(event)
        body = export.zip_bytes()
        now = export.now
    audit.record(AuditAction.EXPORT_DOWNLOADED, request=request, subject=event.slug, event=event.slug,
                 format="zip", sheets=len(SHEETS))
    return _download(body, "application/zip", f"{event.slug}-export-{_stamp(now)}.zip")
