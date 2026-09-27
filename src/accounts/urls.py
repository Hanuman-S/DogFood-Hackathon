from django.urls import path

from accounts import views

app_name = "accounts"

urlpatterns = [
    path("login", views.login_view, name="login"),
    path("logout", views.logout_view, name="logout"),
    path("signup", views.signup_view, name="signup"),
    path("account", views.account_view, name="account"),
    path("account/password", views.password_change, name="password_change"),
    path("account/sessions/<int:session_id>/revoke", views.session_revoke, name="session_revoke"),
    path("account/sessions/revoke-others", views.sessions_revoke_others, name="sessions_revoke_others"),
    path("account/tokens", views.token_create, name="token_create"),
    path("account/tokens/<int:token_id>/revoke", views.token_revoke, name="token_revoke"),
]
