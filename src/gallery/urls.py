"""Gallery URLs.

`/projects` is mounted at the **root** of the URLconf, at exactly that string, because that is what
`.dogfood.toml` advertises as `gallery` and the checker concatenates the path raw. It cannot live
under the `projects/` include: `path("projects/", ...)` matches `/projects/<something>` and would
answer `/projects` only through an `APPEND_SLASH` redirect, which `urllib` would follow -- turning
the measured request into a GET of a different path.

`tests/test_acceptance_contract.py` asserts the advertised string resolves here in one hop.
"""

from django.urls import path

from gallery import views

# Mounted at the root by config/urls.py.
root_urlpatterns = [
    path("projects", views.project_gallery, name="project_gallery"),
]

# Mounted under /events/<slug>/ by config/urls.py.
event_urlpatterns = [
    path("<slug:slug>/projects", views.event_project_gallery, name="event_project_gallery"),
]
