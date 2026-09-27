"""Shared test configuration and fixtures.

Settings overrides do NOT belong here -- they live in `config.settings_test`, because
pytest-django configures Django before this file is imported. See that module for why.
"""

from pathlib import Path

import pytest

from core import clock

# The organizers' real fixture file, located relative to this file rather than to the working
# directory so the suite runs the same from the repo root or from inside the container.
FIXTURES_PATH = Path(__file__).resolve().parent.parent / "acceptance" / "fixtures.json"


@pytest.fixture(autouse=True)
def _reset_clock():
    """Guarantee every test starts on the real clock.

    Autouse and unconditional: a test that installs a fake clock and fails midway must not
    leak that clock into the next test. `core.clock.frozen_at` already restores on exit, but
    relying on every future test to use the context manager correctly is exactly the kind of
    assumption that produces an hour of confused debugging.
    """
    clock.set_clock(None)
    yield
    clock.set_clock(None)


@pytest.fixture
def api_client():
    """DRF test client. Used for the Bearer-token paths."""
    from rest_framework.test import APIClient

    return APIClient()


# --------------------------------------------------------------------------------------
# the shared fixture dataset
# --------------------------------------------------------------------------------------


@pytest.fixture(scope="session")
def fixture_report(django_db_setup, django_db_blocker):
    """Import the organizers' fixture file **once per test session**.

    Importing it per test cost about three seconds a time, which is most of the suite's runtime
    for data that never varies. Loading it once outside the per-test transaction means every
    test still sees it, while each test's own writes are rolled back by the ordinary `db`
    fixture -- so tests remain isolated from each other without re-importing 700 rows.

    Tests use `imported` below rather than this fixture directly, so they also get `db`.
    """
    from seed.importer import FixtureImporter

    with django_db_blocker.unblock():
        yield FixtureImporter(FIXTURES_PATH).run()


@pytest.fixture
def imported(db, fixture_report):
    """The fixture dataset, inside a rolled-back transaction. Returns the import report."""
    return fixture_report


@pytest.fixture
def fixture_event(imported):
    from events.models import Event

    return Event.objects.get(external_id="evt_01")


def bearer(token_plaintext: str) -> dict:
    """Build the header kwargs for a Bearer-authenticated request.

    Mirrors what the acceptance checker sends: a single `Authorization` header and nothing
    else -- no CSRF token, no session cookie.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {token_plaintext}"}
