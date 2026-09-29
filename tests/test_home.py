"""The home page: no fake status lines, the portal at a glance, and Nyan Cat."""

import re
import subprocess
import sys
from pathlib import Path

import pytest
from django.test import Client

ROOT = Path(__file__).resolve().parent.parent
TEMPLATES = ROOT / "src"


def test_no_template_prints_a_fake_ok_status_line():
    offenders = [str(p.relative_to(TEMPLATES)) for p in TEMPLATES.rglob("*.html") if "[ OK ]" in p.read_text(encoding="utf-8")]
    assert offenders == []


@pytest.mark.django_db
def test_home_shows_the_counts_and_what_is_happening(make_event):
    event = make_event(slug="spring-hack")
    page = Client().get("/").content.decode()
    assert 'class="nyan"' in page and "Nyan Cat" in page
    assert "open for submissions" in page and "happening now" in page
    assert f'href="/events/{event.slug}"' in page
    assert "submissions close" in page  # its next date


@pytest.mark.django_db
def test_home_says_so_when_nothing_is_running():
    assert "no event is running right now" in Client().get("/").content.decode()


@pytest.mark.django_db
def test_a_portal_greets_without_status_lines(make_user, client_for):
    page = client_for(make_user(name="Ada Lovelace")).get("/participant/").content.decode()
    assert 'class="portal__hello"' in page and "session authenticated" not in page


def test_the_nyan_template_is_what_the_generator_draws(tmp_path):
    """_nyan.html is generated: editing it by hand would be lost on the next run."""
    template = (TEMPLATES / "templates/_nyan.html").read_text(encoding="utf-8")
    assert "scripts/make_nyan.py" in template
    assert not re.search(r"https?://|style=", template)  # offline, and nothing for the CSP to refuse
    assert template.count("<rect") > 200


def test_the_cat_flies_in_from_the_columns_edge_and_the_rainbow_reaches_it():
    css = (TEMPLATES / "static/css/crt.css").read_text(encoding="utf-8")
    assert "overflow: hidden;\n             container-type: inline-size;" in css  # cut at the column edge
    assert "@keyframes nyan-fly-in { from { transform: translateX(calc(-50cqw - 5%)); }" in css
    template = (TEMPLATES / "templates/_nyan.html").read_text(encoding="utf-8")
    assert 'x="-320"' in template  # drawn far past the picture's left edge


def test_the_portal_is_called_nyanjaro_on_every_page():
    """Nothing people see says DOGFOOD. Identifiers others depend on keep "dogfood" (README)."""
    offenders = []
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8")
        if "DOGFOOD" in text or "@dogfood:~$" in text:
            offenders.append(str(path.relative_to(TEMPLATES)))
    assert offenders == []


@pytest.mark.django_db
def test_every_page_names_nyanjaro():
    page = Client().get("/").content.decode()
    assert "· NYANJARO portal</title>" in page and 'aria-label="NYANJARO"' in page
