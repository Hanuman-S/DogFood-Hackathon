from django.urls import path

from accounts import api

app_name = "accounts_api"

urlpatterns = [
    path("me", api.me, name="me"),
]
