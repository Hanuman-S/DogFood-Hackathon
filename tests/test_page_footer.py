"""Long pages keep their closing buttons ("back to <event>", "assign judges") pinned to the bottom
of the screen (.page-footer in crt.css), so nobody scrolls to the end to leave."""

from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parent.parent / "src"

PAGES = [
    "organizer/templates/organizer/progress.html",
    "organizer/templates/organizer/assignments.html",
    "organizer/templates/organizer/rubric.html",
    "organizer/templates/organizer/results.html",
    "organizer/templates/organizer/voting.html",
    "organizer/templates/organizer/voting_integrity.html",
    "organizer/templates/organizer/comments.html",
    "organizer/templates/organizer/records.html",
    "templates/vote.html",
    "participant/templates/participant/vote.html",
]


@pytest.mark.parametrize("page", PAGES)
def test_the_page_ends_with_a_pinned_footer(page):
    html = (SRC / page).read_text(encoding="utf-8")
    footer = html[html.index('class="actions page-footer"'):]
    assert "back to" in footer[:footer.index("</p>")]


def test_the_footer_is_sticky():
    css = (SRC / "static/css/crt.css").read_text(encoding="utf-8")
    rule = css[css.index(".page-footer {"):]
    assert "position: sticky; bottom: 0;" in rule[:rule.index("}")]


@pytest.mark.django_db
def test_judging_progress_offers_assign_judges_and_the_way_back(make_event, client_for):
    event = make_event()
    page = client_for(event.organizer).get(f"/organizer/events/{event.slug}/progress").content.decode()
    footer = page[page.index('class="actions page-footer"'):]
    footer = footer[:footer.index("</p>")]
    assert "assign judges" in footer and f"back to {event.name}" in footer
