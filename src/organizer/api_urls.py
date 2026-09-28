from django.urls import path

from organizer import bundle, export, results_api, voting

urlpatterns = [
    path("export.csv", export.export_csv, name="api_export_csv"),
    path("export.zip", export.export_zip, name="api_export_zip"),
    path("events/<slug:slug>/bundle", bundle.bundle_api, name="api_event_bundle"),
    path("bundles", bundle.import_api, name="api_bundle_import"),
    path("events/<slug:slug>/votes/tally", voting.tally_api, name="api_vote_tally"),
    path("events/<slug:slug>/results/compute", results_api.compute, name="api_results_compute"),
    path("events/<slug:slug>/results/publish", results_api.publish, name="api_results_publish"),
    path("events/<slug:slug>/results/unpublish", results_api.unpublish, name="api_results_unpublish"),
    path("events/<slug:slug>/results/settings", results_api.settings, name="api_results_settings"),
]
