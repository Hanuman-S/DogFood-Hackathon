from django.urls import path

from organizer import export, voting

urlpatterns = [
    path("export.csv", export.export_csv, name="api_export_csv"),
    path("export.zip", export.export_zip, name="api_export_zip"),
    path("events/<slug:slug>/votes/tally", voting.tally_api, name="api_vote_tally"),
]
