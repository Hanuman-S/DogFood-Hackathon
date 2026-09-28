"""core.slugs.to_slug: typed text becomes a machine name; it is never refused for its shape."""

import pytest

from core.slugs import to_slug


@pytest.mark.parametrize("typed, slug", [
    ("example.org", "example-org"),
    ("Spring Hack 2027", "spring-hack-2027"),
    ("my_event/2027", "my-event-2027"),
    ("a\\b", "a-b"),
    ("--Edge--", "edge"),
    ("Café Déjà Vu", "cafe-deja-vu"),
    ("...", ""),
    ("", ""),
    (None, ""),
])
def test_to_slug(typed, slug):
    assert to_slug(typed) == slug


def test_to_slug_is_cut_to_length_without_a_trailing_hyphen():
    assert to_slug("a" * 59 + " b") == "a" * 59
    assert len(to_slug("x" * 100)) == 60
