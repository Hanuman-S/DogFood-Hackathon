from django.apps import AppConfig


class AccountsConfig(AppConfig):
    name = "accounts"
    verbose_name = "Accounts and sessions"

    def ready(self):
        from accounts import signals  # noqa: F401  (registers the login/logout receivers)
