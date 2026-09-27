"""Reading a result: who may see which snapshot, and the ranking as the pages show it.

Nothing here writes, except `winners_rows`, which records its (audited) download.

Visibility (`EventResultSettings.visibility`, with an active `Publication`):

* public_full    -- everyone sees the full ranking of the published snapshot.
* public_winners -- everyone sees the winners of the published snapshot only.
* private, or nothing published -- the page is a 404 for everyone except the event's organizers
  and platform admins, who see a full preview (the published snapshot, else the latest final,
  else the latest preview).

Organizers always see the full ranking. The event itself must be visible too
(`events.services.get_visible_event`), which the views check first.

Ranks as shown:

* `display_rank` is shared by exact ties: 1 + the number of projects with a strictly higher score,
  after rounding to the engine's `equal_decimals` (the engine's own rule for "equal"). The engine's
  ordinal rank breaks such ties by input order, which is not a ranking anyone decided.
* `tie_group` is the engine's uncertainty group (M2: P(ahead) below the threshold). Only shown when
  a group has more than one project. "No separable winner": the top place is shared by an exact
  tie, or the first project's tie group holds anyone else.
* `raw_rank` is the plain mean's rank from the same snapshot's comparison, shared the same way.
* Winners: every project whose display rank is within the top N (so a tie on the cut brings in
  everyone on it), plus the top project(s) of each track.

No per-judge data is shown on any results page.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from django.core.exceptions import PermissionDenied

from accounts.roles import is_organizer_of
from core import audit
from core.models import AuditAction
from events.models import Track
from projects.models import Project
from scoring.errors import NoFinalResult
from scoring.models import Publication, ResultSnapshot, ResultVisibility, SnapshotKind
from scoring.services import active_publication, latest_final, result_settings

DEFAULT_EQUAL_DECIMALS = 9


@dataclass
class Row:
    project: Project
    track: Track | None
    engine_rank: int
    display_rank: int | None
    score: float | None
    se: float | None
    tie_group: int | None
    tie_size: int
    raw_rank: int | None
    n_reviews: int
    awards: list = field(default_factory=list)

    @property
    def shared(self):
        return self.tie_size > 1


@dataclass
class TrackWinners:
    track: Track
    rows: list


@dataclass
class ResultsPage:
    event: object
    snapshot: ResultSnapshot | None
    publication: Publication | None
    visibility: str
    top_n: int
    full: bool  # the whole ranking (else the winners only)
    preview: bool  # an organizer's view of something the public does not see (or not like this)
    rows: list = field(default_factory=list)
    unranked: list = field(default_factory=list)
    overall_winners: list = field(default_factory=list)
    track_winners: list = field(default_factory=list)
    no_separable_winner: bool = False
    components: int = 1

    @property
    def public_mode(self):
        return ResultVisibility(self.visibility).label


def _shared_ranks(values, decimals):
    """{index: 1 + number strictly above}, over the non-None values rounded to `decimals`."""
    rounded = {i: round(v, decimals) for i, v in values.items() if v is not None}
    return {i: 1 + sum(1 for w in rounded.values() if w > v) for i, v in rounded.items()}


def build_rows(event, snapshot):
    """(ranked rows in display order, unranked rows) of one snapshot. Projects the engine ranked
    that are not rows of this event (a duplicate the importer folded in) are left out."""
    result = snapshot.result
    decimals = (snapshot.engine_config.get("config") or {}).get("equal_decimals", DEFAULT_EQUAL_DECIMALS)
    entries = [p for p in result.get("projects", []) if str(p.get("project_id", "")).isdigit()]
    projects = Project.objects.filter(event=event, pk__in=[int(p["project_id"]) for p in entries]) \
        .select_related("team", "track")
    by_pk = {str(p.pk): p for p in projects}
    entries = [p for p in entries if p["project_id"] in by_pk]

    display = _shared_ranks({i: p.get("score") for i, p in enumerate(entries)}, decimals)
    group_sizes = {}
    for p in entries:
        if p.get("score") is not None and p.get("tie_group") is not None:
            group_sizes[p["tie_group"]] = group_sizes.get(p["tie_group"], 0) + 1

    raw_scores = {}
    comparison = snapshot.comparison or {}
    if "raw_mean" in (comparison.get("methods") or ()):
        raw_by_project = {r["project_id"]: (r.get("scores") or {}).get("raw_mean") for r in comparison.get("rows", ())}
        raw_scores = {i: raw_by_project.get(p["project_id"]) for i, p in enumerate(entries)}
    raw = _shared_ranks(raw_scores, decimals)

    ranked, unranked = [], []
    for i, p in enumerate(entries):
        project = by_pk[p["project_id"]]
        row = Row(
            project=project, track=project.track, engine_rank=p.get("rank") or 0,
            display_rank=display.get(i), score=p.get("score"), se=p.get("se"),
            tie_group=p.get("tie_group"), tie_size=group_sizes.get(p.get("tie_group"), 0) if p.get("score") is not None else 0,
            raw_rank=raw.get(i), n_reviews=p.get("n_reviews") or 0,
        )
        (ranked if row.display_rank is not None else unranked).append(row)
    ranked.sort(key=lambda r: (r.display_rank, r.engine_rank))
    unranked.sort(key=lambda r: r.project.name.lower())
    return ranked, unranked


def winners(rows, top_n):
    """(overall winners, [TrackWinners]) of ranked rows in display order. Marks each row's awards."""
    overall = [r for r in rows if r.display_rank <= top_n]
    for r in overall:
        r.awards.append(f"#{r.display_rank} overall")
    by_track = {}
    for r in rows:
        if r.track is not None:
            by_track.setdefault(r.track.pk, []).append(r)
    tracks = []
    for track_rows in by_track.values():
        best = min(r.display_rank for r in track_rows)
        top = [r for r in track_rows if r.display_rank == best]
        for r in top:
            r.awards.append(f"top of {top[0].track.name}")
        tracks.append(TrackWinners(track=top[0].track, rows=top))
    tracks.sort(key=lambda t: (t.track.order, t.track.pk))
    return overall, tracks


def no_separable_winner(rows):
    if len(rows) < 2:
        return False
    first = rows[0]
    return rows[1].display_rank == first.display_rank or first.tie_size > 1


def _staff_snapshot(event, publication):
    if publication is not None:
        return publication.snapshot
    return (ResultSnapshot.objects.filter(event=event, kind=SnapshotKind.FINAL).order_by("-created_at", "-id").first()
            or ResultSnapshot.objects.filter(event=event).order_by("-created_at", "-id").first())


def results_visible(event, viewer) -> bool:
    """Whether `results_page` would show `viewer` anything (cheap: no ranking is built)."""
    if is_organizer_of(viewer, event):
        return True
    return active_publication(event) is not None and result_settings(event).visibility != ResultVisibility.PRIVATE


def results_page(event, viewer) -> ResultsPage | None:
    """What `viewer` may see of `event`'s results, or None (the view answers 404)."""
    settings = result_settings(event)
    publication = active_publication(event)
    staff = is_organizer_of(viewer, event)
    public = publication is not None and settings.visibility != ResultVisibility.PRIVATE

    if public:
        snapshot = publication.snapshot
        full = staff or settings.visibility == ResultVisibility.PUBLIC_FULL
        preview = staff and settings.visibility != ResultVisibility.PUBLIC_FULL
    elif staff:
        snapshot, full, preview = _staff_snapshot(event, publication), True, True
    else:
        return None

    page = ResultsPage(event=event, snapshot=snapshot, publication=publication, visibility=settings.visibility,
                       top_n=settings.winners_top_n, full=full, preview=preview)
    if snapshot is None:
        return page
    rows, unranked = build_rows(event, snapshot)
    page.overall_winners, page.track_winners = winners(rows, settings.winners_top_n)
    page.no_separable_winner = no_separable_winner(rows)
    page.components = len(snapshot.result.get("components") or ()) or 1
    if full:
        page.rows, page.unranked = rows, unranked
    return page


# --- winners.csv -----------------------------------------------------------------------------------

WINNERS_HEADER = ["award", "rank", "track", "project", "team", "member name", "member email", "snapshot"]


def winners_snapshot(event):
    """The snapshot winners are named from: the published one, else the latest final. None if
    there is no final yet (a preview never names winners)."""
    publication = active_publication(event)
    return publication.snapshot if publication is not None else latest_final(event)


def winners_rows(event, *, actor, origin=None):
    """(snapshot, rows) for winners.csv: one row per award per team member, so a mail merge can
    use it as is (there is no outbound mail; this is how organizers reach winners, in every
    visibility mode). Organizers of the event only; the download is audited."""
    if not is_organizer_of(actor, event):
        raise PermissionDenied("Only the event's organizers can download the winners.")
    snapshot = winners_snapshot(event)
    if snapshot is None:
        raise NoFinalResult("There is no final result yet: compute final results first.")
    rows, _ = build_rows(event, snapshot)
    overall, tracks = winners(rows, result_settings(event).winners_top_n)
    awarded = [("overall", r) for r in overall] + [(f"top of {t.track.name}", r) for t in tracks for r in t.rows]
    out = []
    for award, r in awarded:
        for m in r.project.team.members.select_related("user"):
            out.append([award, r.display_rank, r.track.name if r.track else "", r.project.name,
                        r.project.team.name, m.user.name, m.user.email, snapshot.pk])
    audit.record(AuditAction.WINNERS_EXPORTED, origin=origin, actor=actor, subject=event.slug,
                 snapshot=snapshot.pk, rows=len(out))
    return snapshot, out
