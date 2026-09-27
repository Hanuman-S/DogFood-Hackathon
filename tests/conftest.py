"""Shared test configuration and fixtures.

Settings overrides do NOT belong here -- they live in `config.settings_test`, because
pytest-django configures Django before this file is imported. See that module for why.
"""

import pytest

from core import clock


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


def bearer(token_plaintext: str) -> dict:
    """Build the header kwargs for a Bearer-authenticated request.

    Mirrors what the acceptance checker sends: a single `Authorization` header and nothing
    else -- no CSRF token, no session cookie.
    """
    return {"HTTP_AUTHORIZATION": f"Bearer {token_plaintext}"}
