from django.urls import path

from organizer import export

urlpatterns = [
    path("export.csv", export.export_csv, name="api_export_csv"),
    path("export.zip", export.export_zip, name="api_export_zip"),
]
