"""Core views: the health check and the landing page."""

from __future__ import annotations

from django.db import connection
from django.http import JsonResponse
from django.shortcuts import render

from core import clock


def healthz(request):
    """Liveness + readiness in one endpoint, used by the compose healthcheck.

    It deliberately touches the database. A web process that is listening but cannot reach
    Postgres is not healthy, and compose gating `web` on a check that only proved "gunicorn
    is up" would report a working portal before it could serve a single page.

    Returns 200 with `{"status": "ok"}` or 503 with the failure reason. Public and unauthen-
    ticated: a health probe that needs a credential is a health probe that will be turned
    off. It reveals nothing beyond reachability.
    """
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
            cursor.fetchone()
    except Exception as exc:  # noqa: BLE001 - any failure to reach the DB is unhealthy
        return JsonResponse(
            {
                "status": "unavailable",
                "database": "unreachable",
                "detail": str(exc)[:200],
                "time": clock.iso(clock.now()),
            },
            status=503,
        )

    return JsonResponse(
        {
            "status": "ok",
            "database": "ok",
            "time": clock.iso(clock.now()),
        }
    )


def home(request):
    """Landing page.

    Phase 1 renders the skeleton only. Once the `events` app exists this lists the events
    the caller may see, read from the database -- there is no hardcoded demo content in any
    template, and a portal with nothing seeded renders an honest empty state.
    """
    return render(request, "home.html")
