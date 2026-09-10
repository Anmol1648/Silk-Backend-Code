"""Serialisation for investor discovery.

THE D-13 RULE LIVES HERE, NOT IN THE COMPONENT
----------------------------------------------
Contact and relationship details are shown to internal users and withheld from
founders. That is enforced at the serialiser, so a new frontend screen cannot
leak them by forgetting to check, and a new relationship field cannot be added
without a decision about who sees it — `Investor.RELATIONSHIP_FIELDS` is the
list and this module is the gate.

The founder view omits the block entirely rather than blurring or locking it.
A locked panel advertises that we hold contact details for an investor the
founder is about to approach, which invites a conversation we do not want to
have and tells them nothing useful.
"""
from rest_framework import serializers

from fundos.investors.models import Investor, InvestorShortlist

INTERNAL_ROLES = {"banker", "lead_banker", "fundos_analyst", "fundos_admin",
                  "data_steward"}


def is_internal(user, deal=None):
    """True when this user may see relationship data.

    Deliberately conservative: unknown role means founder-facing.
    """
    if user is None or not getattr(user, "is_authenticated", False):
        return False
    if getattr(user, "is_staff", False) or getattr(user, "is_superuser", False):
        return True
    try:
        from fundos.core.permissions import effective_roles
        roles = effective_roles(user, deal) if deal else set()
    except Exception:
        roles = set()
    if not roles:
        roles = {getattr(user, "role", "") or ""}
    return bool(set(roles) & INTERNAL_ROLES)


def relationship_block(investor):
    """The internal-only block. Never called for a founder audience."""
    return {
        "internalName": investor.rel_internal_name,
        "owner": investor.rel_owner,
        "met": investor.rel_met,
        "relationship": investor.rel_relationship,
        "contactName": investor.rel_contact_name,
        "contactTitle": investor.rel_contact_title,
        "mobile": investor.rel_mobile,
        "emails": investor.rel_emails or [],
        "pastOutreach": investor.rel_past_outreach,
        "pastRevert": investor.rel_past_revert,
        "notes": investor.rel_notes,
    }


def serialise_match(row, *, investor=None, internal=False):
    """One row of the discovery table.

    `trace` always ships. It is what makes "why is this one fourth"
    answerable, and hiding it would leave the ranking looking arbitrary.
    """
    out = {
        "investorId": row["investorId"],
        "name": row["name"],
        "category": row["categoryName"],
        "bucket": row["bucketName"],
        "bucketId": row["bucketId"],
        "hqCountry": row["hqCountry"],
        "avgTicketUsdMn": row["avgTicketUsdMn"],
        "tier": row["tier"],
        "tierLabel": row["tierLabel"],
        "matchPercent": row["matchPercent"],
        "exclusive": row["exclusive"],
        "representativeDeal": row["representativeDeal"],
        "trace": row["trace"],
    }
    if internal and investor is not None:
        out["relationship"] = relationship_block(investor)
    return out


class ShortlistSerializer(serializers.ModelSerializer):
    investorName = serializers.CharField(source="investor.name", read_only=True)
    investorId = serializers.UUIDField(source="investor.id", read_only=True)

    class Meta:
        model = InvestorShortlist
        fields = ["id", "investorId", "investorName", "match_score",
                  "tier_at_save", "rationale", "note", "saved_at"]
        read_only_fields = ["id", "saved_at"]


class InvestorAdminSerializer(serializers.ModelSerializer):
    """Full record for admin-side tools. Includes relationship data."""

    class Meta:
        model = Investor
        fields = "__all__"
