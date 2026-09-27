from django.db import connection
from django.http import HttpResponse, JsonResponse
from django.shortcuts import render

from core.net import wants_json


def healthz(request):
    """200 only when the database answers, so 'healthy' means 'can serve a page'."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


def forbidden(request, exception=None, reason=""):
    reason = reason or "your role does not grant access to this page."
    if wants_json(request):
        return JsonResponse({"error": "forbidden", "detail": reason}, status=403)
    return render(request, "403.html", {"reason": reason}, status=403)


def not_found(request, exception=None):
    if wants_json(request):
        return JsonResponse({"error": "not_found"}, status=404)
    return render(request, "404.html", status=404)


def server_error(request):
    if wants_json(request):
        return JsonResponse({"error": "server_error"}, status=500)
    return render(request, "500.html", status=500)
