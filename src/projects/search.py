"""Full-text search vector maintenance.

The gallery searches one precomputed `tsvector` column rather than running `to_tsvector()` over
four columns and a join at query time. That is what makes the GIN index usable.

**Weights** (Postgres ranks A > B > C > D):

| Weight | Fields | Why |
|--------|--------|-----|
| A | project name | What people actually search for |
| B | tagline, tags, team name | Strong signals, but a name match should still win |
| C | description | Long prose; a single incidental mention should not outrank a title |

**Why the vector is built in Python rather than by a database trigger.** A trigger would be
invisible to the test suite, would need raw SQL in a migration to defend, and would silently
disagree with this weighting if someone changed one and not the other. The cost is that every
write path must call `rebuild_search_vector`, which is a discipline the service layer already
enforces for audit logging and deadline checks.

Note also that the vector is written with `.update()`, which deliberately does **not** touch
`updated_at`: reindexing is not a content edit, and an organizer reading "last updated" should
see when the team last changed something.
"""

from __future__ import annotations

from django.contrib.postgres.search import SearchVector
from django.db import models
from django.db.models import Value

# Stemming and stop-word configuration. 'english' is a reasonable default for a hackathon whose
# submissions are in English; it is named in one place so a self-hoster running a
# non-English event has a single line to change.
SEARCH_CONFIG = "english"


def _text(value: str):
    """Wrap a Python string so it can be fed to SearchVector inside an UPDATE."""
    return Value(value or "", output_field=models.TextField())


def build_search_vector(*, name: str, tagline: str, keywords: str, description: str):
    """Compose the weighted vector expression from already-materialized text."""
    return (
        SearchVector(_text(name), weight="A", config=SEARCH_CONFIG)
        + SearchVector(_text(tagline), weight="B", config=SEARCH_CONFIG)
        + SearchVector(_text(keywords), weight="B", config=SEARCH_CONFIG)
        + SearchVector(_text(description), weight="C", config=SEARCH_CONFIG)
    )


def rebuild_search_vector(project) -> None:
    """Recompute and store one project's search vector.

    The values are read in Python and passed as literals rather than as column references,
    because `team.name` and the tag names live in other tables and Postgres does not allow a
    joined column inside an UPDATE's SET expression. Materializing them here keeps the
    expression valid and the weighting explicit.
    """
    from projects.models import Project

    tag_names = list(project.project_tags.values_list("tag__name", flat=True))
    team_name = project.team.name if project.team_id else ""

    # Tags and team name share weight B; concatenating them into one vector term is equivalent
    # to two terms at the same weight and costs one less SearchVector.
    keywords = " ".join([*tag_names, team_name]).strip()

    Project.objects.filter(pk=project.pk).update(
        search_vector=build_search_vector(
            name=project.name,
            tagline=project.tagline,
            keywords=keywords,
            description=project.description,
        )
    )
