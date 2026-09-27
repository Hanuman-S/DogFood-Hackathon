from django.apps import AppConfig


class ScoringConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "scoring"
    verbose_name = "Scoring rubric and scores (T2 uses these)"
