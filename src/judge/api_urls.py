from django.urls import path

from judge import api

urlpatterns = [
    path("judge/scores", api.judge_scores, name="api_judge_scores"),
]
