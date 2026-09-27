"""The role model, in one place.

**Roles are held per event.** `events.EventMembership(user, event, role)` says that this person is
a participant, judge or organizer *of this event*. The same person may judge one hackathon and
compete in the next, and an organizer's powers stop at the edge of their own events. The only
platform-wide facts about an account are two flags on `User`:

* `is_platform_admin` -- runs the platform: every event, every account, the audit trail.
* `can_create_events` -- may start a new event (and so becomes its first organizer).

"Visitor" is not stored: it is simply anyone who is not logged in.

**Conflict of interest.** Nobody may be a competitor (participant) and staff (judge or
organizer) in the same event. `can_compete_in` is the service-layer check; an exclusion
constraint on `events_eventmembership` is the database backstop (Postgres only; see
events/migrations).

**Portals.** Each audience still has its own portal (its own Django app and URL prefix), exactly
as the design describes. A portal is a *view onto memberships*: `PORTAL_ACCESS` says who may
enter at all, and every event-scoped page inside a portal then checks the role in *that* event.
"""

from django.db import models


class Role(models.TextChoices):
    """A role held in one event."""

    PARTICIPANT = "participant", "Participant"
    JUDGE = "judge", "Judge"
    ORGANIZER = "organizer", "Organizer"


# The two sides of the conflict-of-interest line. Every other reference derives from these.
COMPETITOR_ROLES = frozenset({Role.PARTICIPANT})
STAFF_ROLES = frozenset({Role.JUDGE, Role.ORGANIZER})

ADMIN = "admin"  # the platform flag, not an event role
VISITOR = "visitor"


def is_authenticated(user):
    return bool(user is not None and getattr(user, "is_authenticated", False))


def is_admin(user):
    return is_authenticated(user) and bool(getattr(user, "is_platform_admin", False))


def roles_in(user, event):
    """Every role `user` holds in `event` (admin is not an event role and never appears)."""
    if not is_authenticated(user) or event is None:
        return frozenset()
    from events.models import EventMembership

    return frozenset(
        EventMembership.objects.filter(user=user, event=event).values_list("role", flat=True)
    )


def event_roles(user):
    """Every role `user` holds in any event. Cached on the user object for one request."""
    if not is_authenticated(user):
        return frozenset()
    cached = getattr(user, "_dogfood_event_roles", None)
    if cached is None:
        from events.models import EventMembership

        cached = frozenset(
            EventMembership.objects.filter(user=user).values_list("role", flat=True).distinct()
        )
        user._dogfood_event_roles = cached
    return cached


def forget_cached_roles(user):
    if user is not None and hasattr(user, "_dogfood_event_roles"):
        del user._dogfood_event_roles


def is_organizer_of(user, event):
    return is_admin(user) or Role.ORGANIZER in roles_in(user, event)


def is_judge_of(user, event):
    return Role.JUDGE in roles_in(user, event)


def can_compete_in(user, event):
    """Why `user` may not compete in `event`, as a sentence -- or "" if they may."""
    if not is_authenticated(user):
        return "Log in first."
    if is_admin(user):
        return "Platform admins run every event, so they cannot compete in one."
    staff = roles_in(user, event) & STAFF_ROLES
    if staff:
        return (
            f"You are {'an' if Role.ORGANIZER in staff else 'a'} "
            f"{' and '.join(sorted(staff))} in this event, so you cannot compete in it."
        )
    return ""


# portal name -> who may enter. Checked by `accounts.guards.portal_required` on every request.
PORTAL_ACCESS = {
    # Any account that is not a platform admin: participation is registered by forming or
    # joining a team, and the per-event conflict check happens there.
    "participant": lambda user: is_authenticated(user) and not is_admin(user),
    "judge": lambda user: Role.JUDGE in event_roles(user),
    "organizer": lambda user: (
        is_admin(user)
        or bool(getattr(user, "can_create_events", False))
        or Role.ORGANIZER in event_roles(user)
    ),
    "admin": is_admin,
}

# Most powerful first: a login lands on the first portal the account can enter.
PORTAL_ORDER = ["admin", "organizer", "judge", "participant"]

PORTAL_URL = {
    "participant": "participant:home",
    "judge": "judge:home",
    "organizer": "organizer:home",
    "admin": "platform_admin:home",
}


def can_enter(user, portal):
    return is_authenticated(user) and PORTAL_ACCESS[portal](user)


def portals_of(user):
    """The portals `user` may enter, most powerful first."""
    return [portal for portal in PORTAL_ORDER if can_enter(user, portal)]


def home_portal(user):
    """The portal a login lands on."""
    portals = portals_of(user)
    return portals[0] if portals else "participant"


def role_of(user):
    """A one-word label for audit rows and the status line: 'visitor', or the home portal."""
    if not is_authenticated(user):
        return VISITOR
    return home_portal(user)
