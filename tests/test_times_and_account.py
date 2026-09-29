"""Times are marked up once (the utc filter) so they can be styled; the account page is lean."""

from datetime import datetime, timezone as dt_timezone
from pathlib import Path

import pytest

from core.templatetags.portal import utc


def test_a_time_is_a_time_element_in_utc():
    when = datetime(2026, 10, 28, 19, 33, tzinfo=dt_timezone.utc)
    assert str(utc(when)) == ('<time class="when" datetime="2026-10-28T19:33:00+00:00">'
                              "2026-10-28 19:33 UTC</time>")


def test_an_empty_time_is_plain_text():
    assert utc(None, "to be announced") == "to be announced"
    assert utc(None) == "--"


@pytest.mark.django_db
def test_the_account_page_has_no_event_roles_row(make_user, client_for):
    page = client_for(make_user()).get("/account").content.decode()
    assert "event roles" not in page and ">name<" in page


def test_fields_keep_their_label_on_top_in_a_row_of_uneven_height():
    css = (Path(__file__).resolve().parent.parent / "src/static/css/crt.css").read_text(encoding="utf-8")
    assert ".field { display: grid; gap: 6px; align-content: start; }" in css
