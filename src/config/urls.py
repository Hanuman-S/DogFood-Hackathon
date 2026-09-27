"""Root URL map.

One URL prefix per audience, each served by its own Django app:

    /               public      visitors (no login): home, and later the gallery
    /participant/   participant
    /judge/         judge
    /organizer/     organizer   (admins may enter too)
    /admin/         platform_admin, with the raw database admin at /admin/db/

Access to every prefix is decided in the backend by `accounts.guards.portal_required`, never
by hiding a link.
"""

from django.urls import include, path

from core import views as core_views
from judge import views as judge_views
from participant import views as participant_views
from platform_admin.site import database_admin
from projects import media

urlpatterns = [
    path("healthz", core_views.healthz, name="healthz"),
    path("", include("accounts.urls")),
    path("api/", include("accounts.api_urls")),
    path("api/", include("projects.api_urls")),
    path("api/", include("judge.api_urls")),
    path("api/", include("organizer.api_urls")),
    path("api/", include("participant.api_urls")),
    path("join/<str:token>", participant_views.join, name="join"),
    path("invite/judge/<str:token>", judge_views.invite, name="judge_invite"),
    path("media/projects/<str:name>", media.serve, name="project_media"),
    path("", include("public.urls")),
    path("participant/", include("participant.urls")),
    path("judge/", include("judge.urls")),
    path("organizer/", include("organizer.urls")),
    path("admin/db/", database_admin.urls),
    path("admin/", include("platform_admin.urls")),
]

handler403 = "core.views.forbidden"
handler404 = "core.views.not_found"
handler500 = "core.views.server_error"
