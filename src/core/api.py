"""Small helpers shared by the JSON endpoints."""

import json

from django.http import JsonResponse


class BadRequest(Exception):
    pass


def read_json(request):
    """The request body as a dict; form-encoded bodies are accepted too, for curl -d users."""
    if request.content_type == "application/json":
        try:
            data = json.loads(request.body or b"{}")
        except json.JSONDecodeError as error:
            raise BadRequest(f"Body is not valid JSON: {error.msg}.") from error
        if not isinstance(data, dict):
            raise BadRequest("Body must be a JSON object.")
        return data
    return request.POST.dict()


def error(status, code, detail="", **extra):
    return JsonResponse({"error": code, "detail": detail, **extra}, status=status)


def form_errors(form):
    return error(
        400, "invalid", "Some fields are invalid.",
        fields={name: [str(e) for e in errs] for name, errs in form.errors.items()},
    )
