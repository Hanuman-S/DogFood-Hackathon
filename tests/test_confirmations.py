"""Actions that cannot be undone (or that make something public) ask "are you sure?" first.

The question is a data-confirm attribute on the form, shown by app.js in the portal's own dialog.
This scans every template, so a new form posting to one of these routes without asking fails here.
"""

import re
from pathlib import Path

import pytest

TEMPLATES = Path(__file__).resolve().parent.parent / "src"

# Routes whose forms must ask first.
MUST_ASK = {
    "organizer:part_delete", "organizer:judging_end", "organizer:judge_remove",
    "organizer:organizer_remove", "organizer:judge_invite_revoke", "organizer:extension_revoke",
    "organizer:record_revoke", "organizer:voting_end", "organizer:voting_remove",
    "organizer:voter_link_revoke", "organizer:open_link_rotate", "organizer:ballot_void",
    "organizer:assignment_withdraw", "organizer:judge_reassign", "organizer:results_unpublish",
    "organizer:results_publish", "organizer:event_publish", "participant:team_remove",
    "participant:project_unsubmit", "participant:image_remove", "public:comment_delete",
    "accounts:token_revoke",
}

FORM = re.compile(r"<form\b[^>]*>", re.S)
ACTION = re.compile(r"action=\"\{% url '([a-z_]+:[a-z_]+)'")


def forms():
    for path in TEMPLATES.rglob("*.html"):
        for tag in FORM.findall(path.read_text(encoding="utf-8")):
            match = ACTION.search(tag)
            if match:
                yield path.relative_to(TEMPLATES), match.group(1), tag


def test_every_route_that_must_ask_has_a_form():
    assert MUST_ASK <= {name for _, name, _ in forms()}


@pytest.mark.parametrize("path, name, tag", [f for f in forms() if f[1] in MUST_ASK],
                         ids=lambda v: str(v) if not str(v).startswith("<") else "")
def test_the_form_asks_first(path, name, tag):
    assert 'data-confirm="' in tag, f"{path}: the form posting to {name} does not ask first"


def test_the_judges_decline_asks_first():
    page = (TEMPLATES / "judge/templates/judge/project_score.html").read_text(encoding="utf-8")
    decline = page[page.index('value="decline"') - 400:page.index('value="decline"')]
    assert "data-confirm=" in decline


@pytest.mark.django_db
def test_the_rendered_question_names_the_thing(make_event, client_for):
    event = make_event()
    event.tracks.create(name="Hardware")
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/").content.decode()
    assert 'data-confirm="Delete the track “Hardware”? This cannot be undone."' in page
    assert "data-confirm=\"Publish " in page or "data-confirm=\"Unpublish " in page
