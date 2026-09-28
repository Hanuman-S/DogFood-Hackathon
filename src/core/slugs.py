"""Machine names made from what a person typed: an event's url name, a rubric criterion's key.

Typed text is normalized, never refused for its shape: "example.org" becomes "example-org" and
"Spring Hack 2027" becomes "spring-hack-2027". Only text with no letter or digit at all leaves
nothing, and the caller says so in its own words.
"""

import re

from django.utils.text import slugify

# Dots, underscores and slashes separate words as much as spaces do.
_SEPARATORS = re.compile(r"[._/\\]+")


def to_slug(text, max_length=60):
    """Lower-case letters, digits and single hyphens, at most max_length long; "" if none."""
    # slugify already joins runs of hyphens; cutting to length can leave one at the end.
    return slugify(_SEPARATORS.sub("-", text or ""))[:max_length].strip("-")
