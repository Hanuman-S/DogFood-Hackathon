"""Template context available on every page.

Kept deliberately thin. Anything a specific page needs comes from that page's view; this is
only for things the shared layout renders on every response.
"""

from django.conf import settings

from core import clock


def portal(request):
    return {
        # The layout shows a DEMO MODE banner so that nobody mistakes a seeded demo for a
        # production deployment. An operator who sets DEMO_MODE=0 sees it disappear.
        "demo_mode": settings.DEMO_MODE,
        # Every timestamp in the UI is UTC and labelled as such. Rendering the current UTC
        # time in the footer gives a visitor a reference point for reading deadlines.
        "utc_now": clock.now(),
        "portal_name": "Dogfood Portal",
    }
