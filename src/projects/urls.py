"""Project URLs.

Split into two lists, mounted at two prefixes by `config/urls.py`, because the URLs read the way
people talk about the things they point at: a project is created *inside an event*
(`/events/<slug>/projects/new`), and afterwards it has an identity of its own (`/projects/<id>`).
Phase-4 templates already link to exactly these paths.

Note `/projects` itself (no trailing path) is **not** here -- it is the public gallery, registered
at the root in `config/urls.py` at exactly the string `.dogfood.toml` advertises.
"""

from django.urls import path

from projects import views

# Mounted at /projects/ by config/urls.py.
urlpatterns = [
    path("<int:project_id>", views.project_detail, name="project_detail"),
    path("<int:project_id>/edit", views.project_edit, name="project_edit"),
    path("<int:project_id>/submit", views.project_submit, name="project_submit"),
    path("<int:project_id>/images", views.image_add, name="project_image_add"),
    path("<int:project_id>/thumbnail", views.thumbnail_set, name="project_thumbnail_set"),
    # Serving uploaded bytes. Deliberately a view, not a static route -- see views.py.
    path("<int:project_id>/thumbnail.img", views.thumbnail_serve, name="project_thumbnail"),
    path("images/<int:image_id>", views.image_serve, name="project_image"),
    path("images/<int:image_id>/delete", views.image_remove, name="project_image_remove"),
]

# Mounted under /events/<slug>/ by config/urls.py.
event_project_urlpatterns = [
    path("<slug:slug>/projects/new", views.project_create, name="project_create"),
]
