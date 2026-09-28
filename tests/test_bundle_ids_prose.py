"""Ids written into prose by the engine: every stored f-string that interpolates an id must be listed
in imports.bundle_ids.TEXT_TEMPLATES, so the bundle rewrites it. This reads the engine's source (and
the assignment planner's, whose warnings are stored in AssignmentRound.summary); a new template that
names a project or a judge fails here until it is declared."""

import ast
import re
from pathlib import Path

import pytest

from imports import bundle_ids

SRC = Path(__file__).resolve().parent.parent / "src"
# Code whose strings are stored (in result snapshots, or an assignment round's summary). The CLI
# prints, io.py logs the organizers' file, prepare.py raises: none of their strings is stored.
STORED = [p for p in (SRC / "scoring" / "engine").rglob("*.py") if p.name not in ("cli.py", "io.py", "prepare.py")]
STORED.append(SRC / "scoring" / "assignment.py")

# Names that hold an id (a project, a judge, a review, a duplicate) where the engine interpolates them.
ID_NAMES = {"kept", "dup", "what", "j", "p", "judge", "project", "review", "judge_id", "project_id"}


def _names(expr):
    names = set()
    for node in ast.walk(expr):
        if isinstance(node, ast.Name):
            names.add(node.id)
        elif isinstance(node, ast.Attribute):
            names.add(node.attr)
    return names


def id_bearing_fstrings(source):
    """(skeleton, line) of every f-string outside a `raise` that interpolates an id-like name. The
    skeleton has each field replaced by {}."""
    tree = ast.parse(source)
    in_raise = {id(n) for r in ast.walk(tree) if isinstance(r, ast.Raise) for n in ast.walk(r)}
    found = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.JoinedStr) or id(node) in in_raise:
            continue
        fields = [v for v in node.values if isinstance(v, ast.FormattedValue)]
        if not any(_names(f.value) & ID_NAMES or any(n.endswith("_id") for n in _names(f.value)) for f in fields):
            continue
        skeleton = "".join(v.value if isinstance(v, ast.Constant) else "{}" for v in node.values)
        found.append((skeleton, node.lineno))
    return found


def declared():
    return {re.sub(r"\{\w+\}", "{}", template) for _, template in bundle_ids.TEXT_TEMPLATES}


def test_every_id_bearing_template_in_the_engine_is_declared():
    missing = []
    for path in STORED:
        for skeleton, line in id_bearing_fstrings(path.read_text(encoding="utf-8")):
            if skeleton not in declared():
                missing.append(f"{path.relative_to(SRC)}:{line}  {skeleton!r}")
    assert not missing, "declare these in imports/bundle_ids.TEXT_TEMPLATES:\n" + "\n".join(missing)


def test_the_scanner_finds_an_undeclared_template():
    source = 'excluded.append(Exclusion("review", rid, f"odd reason about {review.project_id} here"))\n'
    assert id_bearing_fstrings(source) == [("odd reason about {} here", 1)]
    assert "odd reason about {} here" not in declared()


def test_the_scanner_ignores_counts_and_errors():
    source = ('notes.append(f"{len(judges)} judges, component {fit.index}")\n'
              'raise ValueError(f"project {project_id} is odd")\n')
    assert id_bearing_fstrings(source) == []


def _mapper(namespace, value, typ):
    return f"{namespace}#{value}"


@pytest.mark.parametrize("text,expected", [
    ("review of duplicate submission dup:prj_41 (kept: 33)", "review of duplicate submission dup:prj_41 (kept: projects#33)"),
    ("judge also reviewed 33, the kept submission; that review is used",
     "judge also reviewed projects#33, the kept submission; that review is used"),
    ("duplicate submission of 33; excluded with its reviews", "duplicate submission of projects#33; excluded with its reviews"),
    ("duplicate submission of 33; its reviews merged into 33",
     "duplicate submission of projects#33; its reviews merged into projects#33"),
    ("by flat judge 16", "by flat judge memberships#16"),
    ("flat judge: all 4 reviews give 3 on every criterion", "flat judge: all 4 reviews give 3 on every criterion"),
    ("no reviews", "no reviews"),
    ("draft, not submitted", "draft, not submitted"),
])
def test_each_template_is_rewritten_and_other_prose_is_kept(text, expected):
    assert bundle_ids.rewrite_text(text, _mapper) == expected


def test_a_missing_row_name_passes_through_a_template():
    assert bundle_ids.rewrite_text("by flat judge missing-memberships#1", lambda ns, v, t: v) == \
        "by flat judge missing-memberships#1"
