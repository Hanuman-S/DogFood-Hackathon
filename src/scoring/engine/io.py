"""Read an organizer-format JSON file (acceptance/fixtures.json, the lab's synthetic sets) into an
EngineInput. Nothing touches the database. Rules ported from the lab's `lab/data.py`:

* **Duplicate submissions** are *declared*, not dropped: projects are linked when they share a
  team, or the same (title stripped and lower-cased, repo_url); in each linked group the one
  submitted first (then lowest id) is kept and the rest map to it in `EngineInput.duplicates`.
  What happens to them is the engine's duplicate policy (S2), not the loader's.
* **Repeated records** of one (judge, project) pair are averaged criterion by criterion into one
  review, in the position of the pair's first record, and logged. The engine itself refuses
  repeats.
* Scores for projects not in the file's project list are dropped and logged.
* The file carries no weights or bounds: each criterion gets weight 1 (or `weights`), min 1 and
  max 5, as the portal's importer does. Criteria are in order of first appearance.
"""

from __future__ import annotations

import json
from collections import defaultdict
from pathlib import Path

from .errors import EngineInputError
from .types import Criterion, EngineInput, Review, Rubric


def load_organizer_file(path, *, weights: dict | None = None) -> tuple[EngineInput, list[str]]:
    """(input, log lines). One event per file."""
    try:
        raw = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise EngineInputError(f"Cannot read {path}: {error}") from error
    if not isinstance(raw, dict) or not isinstance(raw.get("projects"), list):
        raise EngineInputError(f"{path}: not an organizer-format file (no 'projects' list).")
    return from_organizer_dict(raw, weights=weights)


def from_organizer_dict(raw: dict, *, weights: dict | None = None) -> tuple[EngineInput, list[str]]:
    event = raw.get("event") or raw.get("events") or {"id": "implicit"}
    if isinstance(event, list):
        if len(event) != 1:
            raise EngineInputError("The file holds several events; the engine scores one event at a time.")
        event = event[0]
    event_id = str(event.get("id") or "implicit")
    log = [f"event {event_id}"]

    projects = raw["projects"]
    project_ids = [str(p["id"]) for p in projects]
    if len(set(project_ids)) != len(project_ids):
        raise EngineInputError("A project id appears twice in the file.")
    duplicates = {}
    for group in _duplicate_groups(projects):
        kept = str(group[0]["id"])
        for other in group[1:]:
            duplicates[str(other["id"])] = kept
            log.append(f"duplicate: {other['id']} duplicates {kept} (team {group[0].get('team')})")

    known = set(project_ids)
    rows, dropped = [], 0
    for s in raw.get("scores") or []:
        if str(s.get("project")) not in known:
            dropped += 1
            continue
        rows.append((str(s["judge"]), str(s["project"]), s.get("criteria") or {}))
    if dropped:
        log.append(f"scores for unknown projects: {dropped} dropped")

    keys = list(dict.fromkeys(k for _, _, criteria in rows for k in criteria))
    weights = weights or {}
    unknown = sorted(set(weights) - set(keys))
    if unknown:
        raise EngineInputError(f"--weights names criteria not in the file: {', '.join(unknown)}.")
    rubric = Rubric(tuple(Criterion(k, float(weights.get(k, 1.0)), 1.0, 5.0) for k in keys))

    tracks = {str(p["id"]): (str(p["track"]) if p.get("track") is not None else None) for p in projects}
    reviews = [
        Review(judge, project, items, tracks[project])
        for judge, project, items in _collapse_repeats(rows, log)
    ]
    return EngineInput(event_id=event_id, reviews=tuple(reviews), rubric=rubric, projects=tracks,
                       duplicates=duplicates), log


def _collapse_repeats(rows, log):
    """One review per (judge, project): criteria averaged over the pair's records (lab 'mean')."""
    groups = defaultdict(list)
    for judge, project, criteria in rows:
        groups[(judge, project)].append(criteria)
    repeated = sum(1 for records in groups.values() if len(records) > 1)
    if repeated:
        log.append(f"repeats: {repeated} (judge, project) pairs had several records; criteria averaged")
    out = []
    for (judge, project), records in groups.items():
        items = {}
        for key in dict.fromkeys(k for record in records for k in record):
            values = [float(r[key]) for r in records if r.get(key) is not None]
            if values:
                items[key] = sum(values) / len(values)
        out.append((judge, project, items))
    return out


def _duplicate_groups(projects):
    """The lab's rule: same team, or same (title.strip().lower(), repo_url). Title alone is not
    enough. Each group sorted by (submitted_at, id); the first is the one kept."""
    parent = {str(p["id"]): str(p["id"]) for p in projects}

    def find(x):
        while parent[x] != x:
            parent[x] = parent[parent[x]]
            x = parent[x]
        return x

    keys = (
        lambda p: ("team", p.get("team")),
        lambda p: ("title+repo", ((p.get("title") or "").strip().lower(), p.get("repo_url"))),
    )
    for key in keys:
        first = {}
        for p in projects:
            k = key(p)
            if k[1] in (None, "", ("", None)):
                continue
            if k in first:
                parent[find(str(p["id"]))] = find(first[k])
            else:
                first[k] = str(p["id"])
    groups = defaultdict(list)
    for p in projects:
        groups[find(str(p["id"]))].append(p)
    return [
        sorted(g, key=lambda p: (p.get("submitted_at") or "", str(p["id"])))
        for g in groups.values() if len(g) > 1
    ]
