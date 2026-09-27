from django.urls import path

from participant import api

urlpatterns = [
    path("events/<slug:slug>/ballot", api.ballot, name="api_ballot"),
]
