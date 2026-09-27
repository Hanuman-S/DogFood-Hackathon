"""Serializers for the JSON API.

These do shape only -- types, lengths, which keys are accepted. Every rule lives in
`projects.services`, which the UI calls with the same data, so neither door can end up enforcing
something the other does not.

**On the field names.** The portal's own columns are `name` and `tagline`. This endpoint also
accepts `title` and `summary`, because that is the vocabulary of `acceptance/fixtures.json` --
the organizers' documented interchange format for a project -- and a client that can read the
fixture file should be able to post in the same words. The mapping is a fixed, documented alias
table applied to every request identically: nothing here inspects the caller, and no value in a
request can change which branch runs.
"""

from __future__ import annotations

from rest_framework import serializers

from projects.models import TAGLINE_MAX_LENGTH

# Accepted alias -> the portal's own field name.
FIELD_ALIASES = {"title": "name", "summary": "tagline"}


class ProjectWriteSerializer(serializers.Serializer):
    """The body of `POST /api/events/<slug>/projects`.

    Only `name` is required, matching the UI: a draft needs a name and nothing else. What a
    *submitted* project needs is decided by `projects.services.missing_to_submit`, not here.
    """

    name = serializers.CharField(max_length=200)
    tagline = serializers.CharField(max_length=TAGLINE_MAX_LENGTH, required=False, allow_blank=True)
    description = serializers.CharField(required=False, allow_blank=True)
    track = serializers.IntegerField(required=False, allow_null=True)
    repo_url = serializers.CharField(required=False, allow_blank=True)
    demo_video_url = serializers.CharField(required=False, allow_blank=True)
    live_url = serializers.CharField(required=False, allow_blank=True)
    tags = serializers.ListField(
        child=serializers.CharField(max_length=60), required=False, allow_empty=True
    )

    def to_internal_value(self, data):
        renamed = dict(data)
        for alias, field in FIELD_ALIASES.items():
            if alias in renamed and field not in renamed:
                renamed[field] = renamed.pop(alias)
        # A comma-separated string is accepted for tags too, since that is what the HTML form
        # posts and there is no reason for the two doors to disagree about it.
        if isinstance(renamed.get("tags"), str):
            renamed["tags"] = [part for part in renamed["tags"].split(",")]
        return super().to_internal_value(renamed)
