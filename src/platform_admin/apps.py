from django.apps import AppConfig


class PlatformAdminConfig(AppConfig):
    # Not called "admin": that label belongs to django.contrib.admin.
    name = "platform_admin"
    verbose_name = "Admin portal"
