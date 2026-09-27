"""Who reviews what: the assignment plan, computed from the database but written by nobody.

`scoring.services.run_assignment` turns a plan into rows; this module only decides. Everything
here is deterministic for a given seed and database state, so a round can be reproduced exactly.

The method
----------
1. **Hard rules** decide who *may* review a project:
   * a judge covers the project's track (a judge with no tracks covers every track; a project with
     no track can go to any judge);
   * no judge-project pair is assigned twice, and a judge who declined a project (conflict of
     interest) never gets it back;
   * judges excluded from this round (e.g. a stalled judge whose reviews are being reassigned)
     get nothing.
   (Nobody can be both a judge and a competitor in one event -- the database refuses it -- so
   that conflict never reaches this module.)
2. **Balance:** every submitted project is filled up to the review target, one review at a time in
   round-robin order, so a shortage is spread thinly instead of leaving some projects with none.
   Projects with the fewest eligible judges go first. Each review goes to the eligible judge with
   the **lowest load**, never past the load cap. A final pass moves reviews from the busiest
   judges to lighter ones who may take them, so loads differ by at most one wherever the rules
   allow.
3. **Randomness, within those rules:** among equally loaded judges, the one sharing the fewest
   projects with the project's existing reviewers is preferred (so judges meet many different
   colleagues), and any remaining tie is broken by a seeded random draw. Nobody can predict or
   steer who reviews whom, and the seed is stored with the round.
4. **Connectivity:** normalization can only compare two judges' leniency through projects they
   (or colleagues they overlap with) both reviewed. If the judge-project graph falls apart into
   separate islands, each smaller island is linked to the largest: one extra review (a judge of
   one island reviews a project of the other), or two from a judge in neither who may review in
   both. What cannot be linked is reported.
5. Each judge's new projects are **shuffled** into their queue (position bias).
"""

import random
from collections import defaultdict
from dataclasses import dataclass, field

from accounts.roles import Role
from events.models import EventMembership
from projects.models import Project, Status
from scoring.models import Assignment, AssignmentStatus


@dataclass
class Plan:
    new: list = field(default_factory=list)            # [(judge_id, project_id)], in creation order
    bridges: list = field(default_factory=list)        # the subset added only to connect islands
    short: dict = field(default_factory=dict)          # project_id -> reviews still missing
    positions: dict = field(default_factory=dict)      # (judge_id, project_id) -> queue position
    warnings: list = field(default_factory=list)
    islands_before: int = 0
    islands_after: int = 0


class Board:
    """The event's current state, as the plan sees it."""

    def __init__(self, event, exclude_judges=()):
        self.event = event
        self.judges = list(
            EventMembership.objects.filter(event=event, role=Role.JUDGE)
            .select_related("user").prefetch_related("judge_tracks").order_by("id")
        )
        self.excluded = {getattr(j, "pk", j) for j in exclude_judges}
        self.tracks_of = {
            j.pk: ({jt.track_id for jt in j.judge_tracks.all()} or None) for j in self.judges
        }
        self.projects = list(
            Project.objects.filter(event=event, status=Status.SUBMITTED)
            .select_related("track").order_by("id")
        )
        rows = list(
            Assignment.objects.filter(project__event=event)
            .values_list("judge_id", "project_id", "status", "position")
        )
        self.taken = {(j, p) for j, p, s, _ in rows if s in (AssignmentStatus.ASSIGNED, AssignmentStatus.DECLINED)}
        self.active = [(j, p) for j, p, s, _ in rows if s == AssignmentStatus.ASSIGNED]
        self.reviews = defaultdict(int)   # project -> assigned reviews
        self.load = defaultdict(int)      # judge -> assigned reviews
        self.reviewers = defaultdict(set)  # project -> judges
        self.next_position = defaultdict(int)
        for j, p in self.active:
            self.reviews[p] += 1
            self.load[j] += 1
            self.reviewers[p].add(j)
        for j, _, _, pos in rows:
            self.next_position[j] = max(self.next_position[j], pos + 1)

    def may_review(self, judge_id, project):
        if judge_id in self.excluded or (judge_id, project.pk) in self.taken:
            return False
        tracks = self.tracks_of.get(judge_id)
        return tracks is None or project.track_id is None or project.track_id in tracks

    def eligible(self, project):
        return [j.pk for j in self.judges if self.may_review(j.pk, project)]

    # --- graph ----------------------------------------------------------------------------

    def islands(self, pairs):
        """Connected components of the judge-project graph over `pairs`, largest first."""
        parent = {}

        def find(x):
            parent.setdefault(x, x)
            while parent[x] != x:
                parent[x] = parent[parent[x]]
                x = parent[x]
            return x

        for j, p in pairs:
            a, b = find(("j", j)), find(("p", p))
            if a != b:
                parent[a] = b
        groups = defaultdict(set)
        for node in list(parent):
            groups[find(node)].add(node)
        return sorted(groups.values(), key=lambda g: (-len(g), min(g)))


def capacity_warnings(board, target, max_load):
    """Sentences for tracks that cannot reach the target with the judges they have."""
    warnings = []
    by_track = defaultdict(list)
    for project in board.projects:
        by_track[project.track].append(project)
    for track, projects in sorted(by_track.items(), key=lambda kv: (kv[0] is None, getattr(kv[0], "order", 0), getattr(kv[0], "pk", 0))):
        name = track.name if track else "projects without a track"
        judges = {j for p in projects for j in board.eligible(p)} | {
            j for p in projects for j in board.reviewers[p.pk]
        }
        if len(judges) < target:
            warnings.append(
                f"{name}: {len(judges)} judge{'s' if len(judges) != 1 else ''} can review it, "
                f"fewer than the target of {target} reviews per project."
            )
        if max_load is not None:
            needed = sum(max(0, target - board.reviews[p.pk]) for p in projects)
            room = sum(max(0, max_load - board.load[j]) for j in judges)
            if needed > room:
                warnings.append(
                    f"{name}: needs {needed} more review{'s' if needed != 1 else ''}, but its judges "
                    f"have room for {room} under the cap of {max_load}."
                )
    return warnings


def make_plan(event, *, target, max_load=None, seed, exclude_judges=()):
    board = Board(event, exclude_judges)
    rng = random.Random(seed)
    plan = Plan()
    plan.warnings += capacity_warnings(board, target, max_load)
    plan.islands_before = len(board.islands(board.active))

    eligible = {p.pk: board.eligible(p) for p in board.projects}
    need = {p.pk: max(0, target - board.reviews[p.pk]) for p in board.projects}
    order = sorted(board.projects, key=lambda p: (len(eligible[p.pk]), rng.random()))
    # Judges who have reviewed together (share a project): the tie-breaker spreads pairings.
    together = defaultdict(set)
    for p, judges in board.reviewers.items():
        for a in judges:
            together[a] |= judges - {a}

    def place(judge_id, project_id, bridge=False):
        plan.new.append((judge_id, project_id))
        if bridge:
            plan.bridges.append((judge_id, project_id))
        board.taken.add((judge_id, project_id))
        board.load[judge_id] += 1
        board.reviews[project_id] += 1
        for other in board.reviewers[project_id]:
            together[other].add(judge_id)
            together[judge_id].add(other)
        board.reviewers[project_id].add(judge_id)

    def room(judge_id):
        return max_load is None or board.load[judge_id] < max_load

    while any(need.values()):
        progressed = False
        for project in order:
            if not need[project.pk]:
                continue
            candidates = [
                j for j in eligible[project.pk]
                if (j, project.pk) not in board.taken and room(j)
            ]
            if not candidates:
                continue
            reviewers = board.reviewers[project.pk]
            best = min(
                candidates,
                key=lambda j: (board.load[j], len(together[j] & reviewers), rng.random()),
            )
            place(best, project.pk)
            need[project.pk] -= 1
            progressed = True
        if not progressed:
            break
    plan.short = {p: n for p, n in need.items() if n}

    # Rebalance: greedy filling can leave a judge two or more reviews above another (near the
    # end, a project's only candidates may already be busy). Move a review from the busiest
    # judge to a lighter one who may take it, until no such move exists.
    def unplace(judge_id, project_id):
        plan.new.remove((judge_id, project_id))
        board.taken.discard((judge_id, project_id))
        board.load[judge_id] -= 1
        board.reviews[project_id] -= 1
        board.reviewers[project_id].discard(judge_id)

    for _ in range(len(plan.new) * 2):
        moved = False
        for judge_id, project_id in sorted(plan.new, key=lambda jp: (-board.load[jp[0]], jp)):
            lighter = [
                k for k in eligible[project_id]
                if (k, project_id) not in board.taken and room(k)
                and board.load[k] <= board.load[judge_id] - 2
            ]
            if lighter:
                target_judge = min(lighter, key=lambda k: (board.load[k], rng.random()))
                unplace(judge_id, project_id)
                place(target_judge, project_id)
                moved = True
                break
        if not moved:
            break

    # Connectivity: link each smaller island to the largest. The cheapest link is one extra
    # review (a judge of one island reviews a project of the other); failing that, a judge in
    # neither island who may review a project in each links them with two.
    projects_by_id = {p.pk: p for p in board.projects}
    all_judges = [j.pk for j in board.judges]
    unlinkable = set()

    def projects_in(group):
        return [projects_by_id[n[1]] for n in sorted(group) if n[0] == "p" and n[1] in projects_by_id]

    while True:
        islands = [g for g in board.islands(board.active + plan.new)]
        if len(islands) <= 1:
            break
        main = islands[0]
        others = [g for g in islands[1:] if frozenset(g) not in unlinkable]
        if not others:
            break
        island = others[0]

        options = []
        for j in all_judges:
            if not room(j):
                continue
            in_main, in_island = ("j", j) in main, ("j", j) in island
            reach_main = [] if in_main else [p for p in projects_in(main) if board.may_review(j, p)]
            reach_island = [] if in_island else [p for p in projects_in(island) if board.may_review(j, p)]
            if in_main and reach_island:
                options.append((1, board.load[j], rng.random(), j, [reach_island[0]]))
            elif in_island and reach_main:
                options.append((1, board.load[j], rng.random(), j, [reach_main[0]]))
            elif not in_main and not in_island and reach_main and reach_island and (
                max_load is None or board.load[j] + 2 <= max_load
            ):
                options.append((2, board.load[j], rng.random(), j, [reach_main[0], reach_island[0]]))
        if not options:
            unlinkable.add(frozenset(island))
            island_projects = sum(1 for n in island if n[0] == "p")
            island_judges = sum(1 for n in island if n[0] == "j")
            plan.warnings.append(
                f"a group of {island_projects} project(s) and {island_judges} judge(s) shares no "
                "reviews with the rest and could not be linked: normalization cannot compare those "
                "judges with the others. add a judge who covers both tracks."
            )
            continue
        _, _, _, j, projects = min(options)
        for project in projects:
            place(j, project.pk, bridge=True)
    plan.islands_after = len(board.islands(board.active + plan.new))

    # Shuffle each judge's new projects into the end of their queue.
    fresh = defaultdict(list)
    for j, p in plan.new:
        fresh[j].append(p)
    for j in sorted(fresh):
        projects = fresh[j]
        rng.shuffle(projects)
        for offset, p in enumerate(projects):
            plan.positions[(j, p)] = board.next_position[j] + offset
    return plan
