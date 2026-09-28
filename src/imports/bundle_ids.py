"""Where the event bundle finds database ids inside JSON columns, and the guard that keeps the list
honest.

Results and tallies store ids as data (the engine's input uses `str(pk)`). A bundle gives every row
a bundle id ("projects#3"), so the ids inside JSON must be rewritten the same way on export and back
to the new primary keys on import, or an imported results page would point at the wrong projects.

Every location is declared below with one kind:

    plain       one DB id of the namespace, remapped ("25" -> "projects#3", 4 -> "ballots#2")
    composite   "<judge>:<project>" (a review id): both halves remapped; a `dup:` project half is
                external and kept
    external    kept byte for byte: not a DB id (a fixture id such as "dup:prj_41", a criterion
                key, a rubric level)

Bundle ids look like "projects#3" (no colon, so a composite of two bundle ids, "memberships#3:projects#5",
splits unambiguously on its first colon).

A project reference is `plain` unless it is a `dup:<fixture id>` (the engine's name for a folded
duplicate), which is external. An exclusion's id follows its sibling `kind`: "review" -> composite,
"project" -> plain (or external for dup:), "judge" -> plain.

The guard: anywhere else, a key that looks like an id (`id`, `*_id`, `*_ids`, `ids`, `project`,
`judge`, `ballot`) or any digit-string dict key fails the export. An id in an undeclared place would
otherwise travel unmapped and silently point at the wrong row after import.

Free text is not an id location, with one declared exception: an exclusion `reason` from the
engine's duplicate filter names the kept project ("kept: 33"); those four templates are remapped.
Everything else in prose (notes, explanations, warnings) is kept verbatim.

`input_hash` is not remapped: it stays the source install's hash of the source install's input.
"""

import re

# Id namespaces: the bundle section whose ids a JSON location holds.
PROJECT, MEMBERSHIP, TRACK, BALLOT, TALLY, SNAPSHOT = (
    "projects", "memberships", "tracks", "ballots", "tally_snapshots", "result_snapshots",
)

ID_LIKE_KEY = re.compile(r"(^|_)id$|_ids$|^ids$|^project$|^judge$|^ballot$")
DIGITS = re.compile(r"\d+")


class UnknownIdField(Exception):
    """An id-like key or a digit-string key where no id location is declared."""


class DanglingId(Exception):
    """A declared id that names no row of this event."""


# One engine result (a method's output): at snapshot.result and at each comparison.results.<method>.
# Paths are relative; "[]" is any list item, "{key}" is any dict key (the key itself is the id).
RESULT = {
    "projects[].project_id": ("project_ref", PROJECT, str),
    "projects[].track_id": ("plain", TRACK, str),
    "judges[].judge_id": ("plain", MEMBERSHIP, str),
    "excluded[].id": ("exclusion", None, str),
    "excluded[].reason": ("reason_text", PROJECT, str),
    "flags.reviews[].judge": ("plain", MEMBERSHIP, str),
    "flags.reviews[].project": ("project_ref", PROJECT, str),
    "coverage.projects_with_no_reviews[]": ("project_ref", PROJECT, str),
    "coverage.projects_below_min_reviews[]": ("project_ref", PROJECT, str),
    "coverage.reviews_by_judge.{key}": ("plain_key", MEMBERSHIP, str),
    "diagnostics.method.components[].sd_floored_judges[]": ("plain", MEMBERSHIP, str),
}

# (model label, JSON field) -> {path: (kind, namespace, python type of the stored value)}
TABLE = {
    ("scoring.ResultSnapshot", "result"): RESULT,
    ("scoring.ResultSnapshot", "comparison"): {
        "rows[].project_id": ("project_ref", PROJECT, str),
        "rows[].track_id": ("plain", TRACK, str),
        "movers[].project_id": ("project_ref", PROJECT, str),
        **{f"results.{{method}}.{path}": spec for path, spec in RESULT.items()},
    },
    ("scoring.ResultSnapshot", "combined"): {
        "[].project_id": ("project_ref", PROJECT, str),
    },
    ("scoring.ResultSnapshot", "diagnostics"): {
        "vote_tally.id": ("plain", TALLY, int),
        "vote_tally.previous": ("plain", TALLY, int),
        "vote_tally.voided_since_previous[]": ("plain", BALLOT, int),
        "vote_tally.restored_since_previous[]": ("plain", BALLOT, int),
        "previous_final.id": ("plain", SNAPSHOT, int),
        "live_tally[].project_id": ("project_ref", PROJECT, str),
    },
    ("scoring.ResultSnapshot", "rubric"): {
        "criteria[].id": ("external", None, str),  # a criterion key, not a database id
    },
    ("scoring.ResultSnapshot", "engine_config"): {},
    ("scoring.ResultSnapshot", "final_weights"): {
        "judge": ("external", None, int),  # the judges' weight (0-100), not an id
    },
    ("voting.VoteTallySnapshot", "rows"): {
        "[].project_id": ("project_ref", PROJECT, str),
    },
    ("voting.VoteTallySnapshot", "counts"): {},
    ("voting.VoteTallySnapshot", "voided_ballot_ids"): {"[]": ("plain", BALLOT, int)},
    ("voting.VoteTallySnapshot", "voided_since_previous"): {"[]": ("plain", BALLOT, int)},
    ("voting.VoteTallySnapshot", "restored_since_previous"): {"[]": ("plain", BALLOT, int)},
    ("scoring.AssignmentRound", "summary"): {
        "short.{key}": ("plain_key", PROJECT, str),
        "excluded_judges[]": ("plain", MEMBERSHIP, int),
    },
    ("scoring.Criterion", "level_descriptions"): {"{key}": ("external_key", None, str)},  # levels "1".."5"
    ("scoring.EventScoringConfig", "overrides"): {},
}

# Paths under "results.{method}" match any method name; everything else matches literally.
_METHOD = re.compile(r"^results\.[^.\[\]{}]+\.")

# The duplicate filter's reason templates (scoring/engine/filters.py), each naming the kept project.
REASON_PROJECT = [
    re.compile(r"(?<=\(kept: )(\d+)(?=\))"),
    re.compile(r"(?<=judge also reviewed )(\d+)(?=, the kept submission)"),
    re.compile(r"(?<=duplicate submission of )(\d+)(?=;)"),
    re.compile(r"(?<=its reviews merged into )(\d+)$"),
]


def _spec(specs, path):
    if path in specs:
        return specs[path]
    generic = _METHOD.sub("results.{method}.", path, count=1)
    return specs.get(generic)


def rewrite(value, specs, mapper, where):
    """Walk one JSON value; return a copy with every declared id passed through `mapper(namespace,
    id, python_type)`. Raises UnknownIdField for an id-like key or a digit key outside `specs`."""
    return _walk(value, "", specs, mapper, where)


def _join(path, key):
    return f"{path}.{key}" if path else key


def _scalar(item):
    return not isinstance(item, (dict, list))


def _walk(value, path, specs, mapper, where):
    if isinstance(value, dict):
        out = {}
        key_spec = _spec(specs, _join(path, "{key}"))
        for key, item in value.items():
            key_str = str(key)
            if key_spec is not None:
                kind, namespace, typ = key_spec
                new_key = mapper(namespace, key_str, typ) if kind == "plain_key" else key
                child = _join(path, "{key}")
            else:
                if DIGITS.fullmatch(key_str):
                    raise UnknownIdField(f"{where}: digit-string key {key_str!r} under {path or '(top)'!r} "
                                         "is not a declared id location")
                new_key, child = key, _join(path, key_str)
            # Under an id-keyed dict the key is the id; the value is data (a count, a text), walked on.
            spec = None if key_spec is not None else _spec(specs, child)
            if spec is not None and _scalar(item):
                out[new_key] = _apply(spec, item, value, mapper)
                continue
            if spec is None and key_spec is None and ID_LIKE_KEY.search(key_str) and item not in (None, [], {})                     and _spec(specs, child + "[]") is None:
                raise UnknownIdField(f"{where}: id-like key {child!r} is not a declared id location")
            out[new_key] = _walk(item, child, specs, mapper, where)
        return out
    if isinstance(value, list):
        item_path = path + "[]"
        spec = _spec(specs, item_path)
        return [_apply(spec, item, None, mapper) if spec is not None and _scalar(item)
                else _walk(item, item_path, specs, mapper, where) for item in value]
    return value


def _apply(spec, item, parent, mapper):
    kind, namespace, typ = spec
    if item is None or kind in ("external", "external_key"):
        return item
    if kind == "plain":
        return mapper(namespace, item, typ)
    if kind == "project_ref":
        return item if str(item).startswith("dup:") else mapper(PROJECT, item, typ)
    if kind == "composite":
        return composite(item, mapper)
    if kind == "exclusion":
        what = (parent or {}).get("kind")
        if what == "review":
            return composite(item, mapper)
        if what == "judge":
            return mapper(MEMBERSHIP, item, typ)
        return item if str(item).startswith("dup:") else mapper(PROJECT, item, typ)
    if kind == "reason_text":
        text = item
        for pattern in REASON_PROJECT:
            text = pattern.sub(lambda m: str(mapper(PROJECT, m.group(1), str)), text)
        return text
    raise AssertionError(kind)


def composite(value, mapper):
    """"<judge>:<project>" -> both halves remapped; a dup:<fixture id> project half is kept."""
    judge, sep, project = str(value).partition(":")
    if not sep:
        raise UnknownIdField(f"not a review id: {value!r}")
    project = project if project.startswith("dup:") else mapper(PROJECT, project, str)
    return f"{mapper(MEMBERSHIP, judge, str)}:{project}"


def json_fields(model_label):
    return [field for (label, field) in TABLE if label == model_label]
