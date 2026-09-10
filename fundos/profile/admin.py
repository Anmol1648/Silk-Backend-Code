from django.contrib import admin
from django.utils.html import format_html

from fundos.profile.models import (ProfileAssessmentInput,
                                   ProfileGenerationRun, ProfileRunEvent,
                                   Investor as _Investor)


@admin.register(_Investor)
class InvestorAdmin(admin.ModelAdmin):
    list_display = ("name", "investor_type", "holder_category",
                    "ownership_pct", "profile")
    list_filter = ("investor_type", "holder_category")
    search_fields = ("name",)


class ProfileRunEventInline(admin.TabularInline):
    """The run's activity log, in order.

    Inline rather than a separate page because it is only ever read in the
    context of one run: "the run took nineteen minutes" is the question, and
    "research batch 7 took eleven of them" is the answer.
    """

    model = ProfileRunEvent
    extra = 0
    can_delete = False
    fields = ("t_plus_seconds", "stage", "message")
    readonly_fields = fields
    ordering = ("at", "id")

    def has_add_permission(self, request, obj=None):
        return False


@admin.register(ProfileGenerationRun)
class ProfileGenerationRunAdmin(admin.ModelAdmin):
    """One row per generation. This is the table to look at when asking
    'why did this profile come out thin?' — it records what the model had
    to work with, not just what it cost.

    The dossier link is the important one. It is the exact source material the
    profile was derived from, in the words of the sources that produced it, so
    a disputed figure can be traced to the sentence it came from instead of
    being re-litigated by re-running the job.
    """
    list_display = ("created_at", "profile", "status", "stage", "forced",
                    "batches", "dossier_size", "generated_count",
                    "needs_input_count", "llm_calls", "total_search_count",
                    "total_cost_inr", "duration_ms")
    list_filter = ("status", "mode", "forced", "created_at")
    date_hierarchy = "created_at"
    inlines = [ProfileRunEventInline]
    readonly_fields = tuple(
        f.name for f in ProfileGenerationRun._meta.fields) + ("dossier_link",)

    @admin.display(description="Batches")
    def batches(self, obj):
        if not obj.batches_total:
            return "—"
        failed = len(obj.batches_failed or [])
        label = f"{obj.batches_succeeded}/{obj.batches_total}"
        return f"{label} ({failed} failed)" if failed else label

    @admin.display(description="Dossier")
    def dossier_size(self, obj):
        return f"{obj.dossier_chars:,}" if obj.dossier_chars else "—"

    @admin.display(description="Dossier download")
    def dossier_link(self, obj):
        """A short-lived signed link to this run's evidence file."""
        if not obj.dossier_uri:
            return "— (no dossier was stored for this run)"
        try:
            from fundos.docs.storage import get_storage
            url = get_storage().signed_url(obj.dossier_uri, expires_sec=900)
        except Exception as exc:
            return f"unavailable: {exc}"
        return format_html('<a href="{}" target="_blank">consolidated.md</a> '
                           '<span style="color:#666">(link valid 15 minutes)'
                           '</span>', url)

    @admin.display(description="Input chars")
    def input_size(self, obj):
        return (obj.source_stats or {}).get("total_chars", 0)

    @admin.display(description="Generated")
    def generated_count(self, obj):
        return len(obj.sections_generated or [])

    @admin.display(description="Needs input")
    def needs_input_count(self, obj):
        return len(obj.sections_needing_input or [])


@admin.register(ProfileAssessmentInput)
class ProfileAssessmentInputAdmin(admin.ModelAdmin):
    """The Phase 3 bridge, made inspectable.

    This is the table to open when a scorecard scores blank on something the
    company obviously publishes: either the row is absent (the extraction did
    not find it) or it is present with a low confidence and a source URL that
    turns out to say something else. Both are answerable in seconds here and
    almost unanswerable from the scorecard alone.

    `is_founder_confirmed` is editable because a founder correction is the
    highest-quality signal in the system and must be settable by someone
    handling a support request, not only through the founder's own UI. It is
    also the one flag that stops a later research run overwriting the value.
    """
    list_display = ("input_key", "value", "unit", "source_type",
                    "source_tier", "confidence", "is_founder_confirmed",
                    "profile", "updated_at")
    list_filter = ("source_type", "source_tier", "is_founder_confirmed",
                   "unit")
    search_fields = ("input_key", "value", "source_url", "profile__id")
    list_editable = ("is_founder_confirmed",)
    readonly_fields = ("created_at", "updated_at")
    list_per_page = 50
