from django import template

from core import markdown

register = template.Library()


@register.filter(name="markdown")
def markdown_filter(text):
    return markdown.render(text)


@register.filter
def utc(value, empty="--"):
    """Every time the portal shows is UTC, and says so. `{{ when|utc:"to be announced" }}`
    names what an empty date means."""
    if not value:
        return empty
    return value.strftime("%Y-%m-%d %H:%M UTC")


@register.filter
def missing_q(missing, question_pk):
    """The 'missing' sentence for a custom question, keyed q_<pk>."""
    return (missing or {}).get(f"q_{question_pk}")


@register.filter
def duration(seconds):
    """3725 -> '1h 2m 5s'; 42 -> '42s'."""
    if seconds is None:
        return ""
    seconds = int(seconds)
    days, rest = divmod(seconds, 86400)
    hours, rest = divmod(rest, 3600)
    minutes, secs = divmod(rest, 60)
    parts = [f"{days}d"] if days else []
    if hours or days:
        parts.append(f"{hours}h")
    if minutes or hours or days:
        parts.append(f"{minutes}m")
    parts.append(f"{secs}s")
    return " ".join(parts)


@register.filter
def iso(value):
    return value.isoformat().replace("+00:00", "Z") if value else ""


@register.filter
def initials(name):
    """'Glass Signal' -> 'GS': the placeholder shown when a project has no thumbnail."""
    letters = [word[0] for word in (name or "").split() if word[:1].isalnum()]
    return "".join(letters[:2]).upper() or "?"


@register.simple_tag
def gallery_url(filters, **overrides):
    query = filters.as_query(**overrides)
    return f"/projects?{query}" if query else "/projects"


@register.filter
def get_item(mapping, key):
    """`{{ load|get_item:judge.pk }}`: a dict lookup by a variable key."""
    try:
        return mapping.get(key)
    except AttributeError:
        return None
