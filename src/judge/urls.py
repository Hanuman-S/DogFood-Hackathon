from django.urls import path

from judge import views

app_name = "judge"

urlpatterns = [
    path("", views.home, name="home"),
]
