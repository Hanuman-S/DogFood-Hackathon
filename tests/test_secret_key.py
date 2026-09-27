"""SECRET_KEY: generated per install into a volume when none is given, refused when known outside
demo mode, and never used directly: each purpose gets its own derived key (core.keys)."""

import pytest
from django.test import override_settings

from config import secret_key
from core.keys import derived_key, keyed_hash
from core.net import hash_ip


def test_two_fresh_installs_get_different_keys_and_one_install_keeps_its_own(tmp_path):
    first = secret_key.ensure_key_file(str(tmp_path / "a" / "secret_key"))
    second = secret_key.ensure_key_file(str(tmp_path / "b" / "secret_key"))
    assert first != second and len(first) >= 64
    assert secret_key.ensure_key_file(str(tmp_path / "a" / "secret_key")) == first  # a restart keeps it
    assert secret_key.check(first, demo_mode=False) == ""


@pytest.mark.django_db
def test_two_fresh_installs_give_different_tokens_for_the_same_link_id(tmp_path):
    from voting import links
    from voting.models import VoterLink

    link = VoterLink(pk=7, nonce="same-nonce")
    tokens = []
    for name in ("a", "b"):
        key = secret_key.ensure_key_file(str(tmp_path / name / "secret_key"))
        with override_settings(SECRET_KEY=key):
            tokens.append(links.link_token(link))
            assert links.link_token(link) == tokens[-1]  # stable within one install
    assert tokens[0] != tokens[1]


def test_known_or_short_keys_are_refused_outside_demo_mode():
    for key in secret_key.KNOWN_DEFAULT_KEYS:
        assert secret_key.check(key, demo_mode=False)
        assert secret_key.check(key, demo_mode=True) == ""
    assert secret_key.check("too-short", demo_mode=False)
    assert secret_key.check("", demo_mode=True)


def test_each_purpose_has_its_own_derived_key():
    with override_settings(SECRET_KEY="k" * 64):
        assert derived_key("voter-links") != derived_key("ip-hash") != derived_key("open-link")
        assert keyed_hash("voter-links", "1.2.3.4") != hash_ip("1.2.3.4")
        assert hash_ip("1.2.3.4") == keyed_hash("ip-hash", "1.2.3.4")


def test_compose_no_longer_ships_a_key():
    from pathlib import Path

    compose = (Path(__file__).resolve().parent.parent / "docker-compose.yml").read_text(encoding="utf-8")
    assert "insecure-compose-default" not in compose
    assert "DJANGO_SECRET_KEY: ${DJANGO_SECRET_KEY:-}" in compose
    assert "secrets:/app/secrets" in compose
