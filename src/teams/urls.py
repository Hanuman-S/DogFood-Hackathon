"""Team URLs.

`/invite/<token>` sits at the root rather than under `/teams/`, because it is a link people paste
into chat and a short path is a link that survives being wrapped, truncated or read aloud.
"""

from django.urls import path

from teams import views

# Mounted at /teams/ by config/urls.py.
urlpatterns = [
    path("<int:team_id>", views.team_detail, name="team_detail"),
    path("<int:team_id>/invites", views.invite_create, name="invite_create"),
    path("<int:team_id>/invites/<int:invite_id>/revoke", views.invite_revoke, name="invite_revoke"),
    path("<int:team_id>/leave", views.team_leave, name="team_leave"),
    path("<int:team_id>/captain", views.captain_transfer, name="captain_transfer"),
]

# Mounted at the root by config/urls.py.
invite_urlpatterns = [
    path("invite/<str:token>", views.invite_accept, name="invite_accept"),
]

# Mounted under /events/<slug>/ by config/urls.py.
event_team_urlpatterns = [
    path("<slug:slug>/teams/new", views.team_create, name="team_create"),
]
