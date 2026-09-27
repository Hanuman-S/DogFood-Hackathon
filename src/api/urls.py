"""API URLs.

Registered at exactly the strings `.dogfood.toml` advertises, with **no trailing slash**. The
acceptance checker uses `urllib`, which follows redirects and rewrites a POST into a GET on a 301
or 302 -- so a route that answered only via Django's `APPEND_SLASH` would be measured as a GET to
a different path. `tests/test_acceptance_contract.py` asserts each advertised path answers in one
hop.

T2's routes (`/api/judge/scores`, `/api/events/<slug>/export.csv`) are deliberately absent. They
404 until T2 implements them, and the four T2 acceptance checks are expected to FAIL until then.
"""

from django.urls import path

from api import views

urlpatterns = [
    path("events/<slug:slug>/projects", views.ProjectCreateView.as_view(), name="api_project_create"),
]
