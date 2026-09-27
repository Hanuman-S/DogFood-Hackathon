from django.urls import path

from platform_admin import views

app_name = "platform_admin"

urlpatterns = [
    path("", views.home, name="home"),
    path("accounts/new", views.account_create, name="account_create"),
]
