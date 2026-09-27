"""Account URLs.

Registered at the root in `config/urls.py` (so `/login`, not `/accounts/login`) because these are
the paths the base template and `settings.LOGIN_URL` advertise, and short auth paths are what
people expect to be able to type.
"""

from django.urls import path

from accounts import views

urlpatterns = [
    path("signup", views.signup, name="signup"),
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("profile", views.profile, name="profile"),
    path("profile/password", views.password_change, name="password_change"),
    path("profile/tokens/<int:token_id>/revoke", views.revoke_token, name="revoke_token"),
]
