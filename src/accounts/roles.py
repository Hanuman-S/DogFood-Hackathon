"""The role model, in one place.

Every account holds exactly one role. "Visitor" is not stored: it is simply anyone who is not
logged in. Each role has a home portal (its own URL prefix and Django app), and
`PORTAL_ACCESS` is the one table that says who may enter which portal.
"""

from django.db import models


class Role(models.TextChoices):
    PARTICIPANT = "participant", "Participant"
    JUDGE = "judge", "Judge"
    ORGANIZER = "organizer", "Organizer"
    ADMIN = "admin", "Admin"


VISITOR = "visitor"

# portal name -> roles allowed in. Judges and participants are kept strictly apart: a judge
# never sees the participant portal and vice versa. Admins may enter the organizer portal,
# because an admin holds every organizer power (see the role-isolation matrix in the brief).
PORTAL_ACCESS = {
    "participant": frozenset({Role.PARTICIPANT}),
    "judge": frozenset({Role.JUDGE}),
    "organizer": frozenset({Role.ORGANIZER, Role.ADMIN}),
    "admin": frozenset({Role.ADMIN}),
}

# role -> the URL name of the page a user lands on after logging in.
HOME_PORTAL_URL = {
    Role.PARTICIPANT: "participant:home",
    Role.JUDGE: "judge:home",
    Role.ORGANIZER: "organizer:home",
    Role.ADMIN: "platform_admin:home",
}


def role_of(user):
    """The role name for any user object, including AnonymousUser ('visitor')."""
    if user is None or not user.is_authenticated:
        return VISITOR
    return user.role
