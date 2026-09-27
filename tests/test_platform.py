"""Boot commands, health, security headers and the offline rule."""

import re
from io import StringIO
from pathlib import Path

import pytest
from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import Client, override_settings

from accounts.models import ApiToken, User
from accounts.roles import Role

pytestmark = pytest.mark.django_db

SRC = Path(__file__).resolve().parent.parent / "src"

DEMO_TOKENS = {
    "admin": "demo-admin-token",
    "organizer": "demo-organizer-token",
    "judge_a": "demo-judge-a-token",
    "judge_b": "demo-judge-b-token",
    "participant": "demo-participant-token",
}


@override_settings(DEMO_MODE=True, DEMO_TOKENS=DEMO_TOKENS)
def test_seed_demo_creates_one_account_per_role_and_working_tokens():
    out = StringIO()
    call_command("seed_demo", stdout=out)
    assert set(User.objects.values_list("role", flat=True)) == set(Role.values)
    assert User.objects.filter(role=Role.JUDGE).count() == 2
    for key, raw in DEMO_TOKENS.items():
        response = Client().get("/api/me", HTTP_AUTHORIZATION=f"Bearer {raw}")
        assert response.status_code == 200, key
    assert 'judge_a     = "Authorization: Bearer demo-judge-a-token"' in out.getvalue()


@override_settings(DEMO_MODE=True, DEMO_TOKENS=DEMO_TOKENS)
def test_seed_demo_is_create_only():
    call_command("seed_demo", stdout=StringIO())
    admin = User.objects.get(email="admin@dogfood.local")
    admin.set_password("changed-by-a-human-1")
    admin.save()
    call_command("seed_demo", stdout=StringIO())
    admin.refresh_from_db()
    assert admin.check_password("changed-by-a-human-1")
    assert User.objects.count() == 5
    assert ApiToken.objects.count() == 5


@override_settings(DEMO_MODE=False)
def test_seed_demo_refuses_outside_demo_mode():
    with pytest.raises(CommandError):
        call_command("seed_demo", stdout=StringIO())
    assert not User.objects.exists()


def test_create_account_command():
    call_command(
        "create_account", email="Boss@Example.org", name="Boss", role="admin",
        password="a-long-enough-pass", stdout=StringIO(),
    )
    assert User.objects.get(email="boss@example.org").role == Role.ADMIN


def test_healthz():
    assert Client().get("/healthz").content == b"ok"


def test_security_headers():
    response = Client().get("/login")
    csp = response["Content-Security-Policy"]
    assert "default-src 'self'" in csp and "frame-ancestors 'none'" in csp
    assert response["X-Frame-Options"] == "DENY"
    assert response["X-Content-Type-Options"] == "nosniff"


def test_database_rejects_an_unknown_role():
    from django.db import IntegrityError, transaction

    with pytest.raises(IntegrityError), transaction.atomic():
        User.objects.create_user("x@example.org", "a-long-enough-pass", name="X", role="superhero")


def test_templates_and_static_reference_no_external_host():
    """The offline rule, checked: no template, stylesheet or script names another host."""
    pattern = re.compile(r"""(?:src|href|url)\s*[=(]\s*["']?(?:https?:)?//""", re.I)
    offenders = []
    for path in list(SRC.rglob("*.html")) + list(SRC.rglob("*.css")) + list(SRC.rglob("*.js")):
        if "staticfiles" in path.parts:
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []


def test_no_inline_styles_or_scripts_that_the_csp_would_block():
    offenders = []
    for path in SRC.rglob("*.html"):
        if path.name == "500.html":
            continue
        text = path.read_text(encoding="utf-8")
        if re.search(r"\sstyle=|<style|<script>(?!</script>)|\son[a-z]+=", text):
            offenders.append(str(path.relative_to(SRC)))
    assert offenders == []
