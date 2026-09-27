from django.contrib import admin

from platform_admin.site import database_admin
from scoring.admin import ReadOnlyAdmin

from .models import Ballot, VotingConfig


@admin.register(VotingConfig, site=database_admin)
class VotingConfigAdmin(ReadOnlyAdmin):
    """Changed only through the organizer's voting page (voting.services, audited)."""

    list_display = ["event", "opens_at", "closes_at", "method", "credit_budget", "access_mode"]
    exclude = ["ballot_secret"]


@admin.register(Ballot, site=database_admin)
class BallotAdmin(ReadOnlyAdmin):
    """Ballots are records: read-only here, never deleted. The window trigger does not guard
    DELETE (ballots must go with their event), so this admin must not offer one; an organizer
    voids a ballot instead (audited, kept)."""

    list_display = ["id", "event", "voter_user", "created_at", "updated_at", "voided_at"]
    list_filter = ["event"]
