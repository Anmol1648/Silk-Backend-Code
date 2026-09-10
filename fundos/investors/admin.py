"""Django admin — the configuration and operations surface.

Two principles from the brief are load-bearing here:

  * ALL CONFIGURATION LIVES IN ADMIN. Nothing tuneable is a Python constant.
    The bar is not "will this change" but "would a change require a
    developer". Raise bands, ranking weights, sector aliases, category
    mappings, the fuzzy threshold, the refresh cadence — all rows.

  * THE REFRESH MUST BE TESTABLE. Dry run, scoped run, stage isolation and
    commit-from-diff are admin actions, not shell commands, because the person
    who needs to check a refresh before it lands is not necessarily the person
    with SSH access.
"""
import logging

from django.contrib import admin, messages
from django.http import HttpResponseRedirect
from django.template.response import TemplateResponse
from django.urls import path, reverse
from django.utils.html import format_html

from fundos.investors.models import (CanonicalSector, ClassificationReviewItem,
                                     DataSource, FundingDeal, Investor,
                                     InvestorAliasCandidate, InvestorBucket,
                                     InvestorCategory, InvestorDeal,
                                     InvestorShortlist, MatchConfig, RaiseBand,
                                     RefreshRun, RefreshSchedule, SectorAlias,
                                     SectorGroup, SectorReviewItem)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Sector taxonomy
# ---------------------------------------------------------------------------

class SectorAliasInline(admin.TabularInline):
    model = SectorAlias
    extra = 1
    fields = ["alias", "created_by_admin"]


@admin.register(CanonicalSector)
class CanonicalSectorAdmin(admin.ModelAdmin):
    list_display = ["name", "level", "parent", "alias_count", "is_active"]
    list_filter = ["level", "is_active"]
    search_fields = ["name", "aliases__alias"]
    inlines = [SectorAliasInline]

    @admin.display(description="Aliases")
    def alias_count(self, obj):
        return obj.aliases.count()

    def save_model(self, request, obj, form, change):
        super().save_model(request, obj, form, change)
        from fundos.investors import normalizer
        normalizer.invalidate()


@admin.register(SectorGroup)
class SectorGroupAdmin(admin.ModelAdmin):
    list_display = ["name", "sector_count"]
    filter_horizontal = ["sectors"]

    @admin.display(description="Sectors")
    def sector_count(self, obj):
        return obj.sectors.count()


@admin.register(SectorReviewItem)
class SectorReviewItemAdmin(admin.ModelAdmin):
    """The unknowns queue.

    The supplied normalizer appended these to a log file — the right instinct,
    the wrong destination. A log is written and never read. Here an admin can
    clear the queue, and one action creates the alias that stops the next
    occurrence.
    """

    list_display = ["raw_value", "best_candidate", "best_score", "occurrences",
                    "status", "last_seen"]
    list_filter = ["status"]
    search_fields = ["raw_value"]
    ordering = ["-occurrences"]
    actions = ["accept_best_match", "mark_ignored"]

    @admin.action(description="Create alias to the best candidate")
    def accept_best_match(self, request, queryset):
        from fundos.investors import normalizer
        created = 0
        for item in queryset.filter(status="open"):
            sector = CanonicalSector.objects.filter(
                name=item.best_candidate).first()
            if not sector:
                self.message_user(
                    request,
                    f"No canonical sector named {item.best_candidate!r} for "
                    f"{item.raw_value!r} — create it first.",
                    level=messages.WARNING)
                continue
            SectorAlias.objects.get_or_create(
                alias=item.raw_value.lower(),
                defaults={"sector": sector, "created_by_admin": True})
            item.status = "resolved"
            item.save(update_fields=["status"])
            created += 1
        normalizer.invalidate()
        self.message_user(request, f"{created} alias(es) created.")

    @admin.action(description="Ignore")
    def mark_ignored(self, request, queryset):
        n = queryset.update(status="ignored")
        self.message_user(request, f"{n} item(s) ignored.")


# ---------------------------------------------------------------------------
# Investor taxonomy
# ---------------------------------------------------------------------------

@admin.register(InvestorBucket)
class InvestorBucketAdmin(admin.ModelAdmin):
    list_display = ["name", "sort_order", "category_count"]

    @admin.display(description="Source categories")
    def category_count(self, obj):
        return obj.categories.count()


@admin.register(InvestorCategory)
class InvestorCategoryAdmin(admin.ModelAdmin):
    list_display = ["name", "bucket", "investor_count"]
    list_filter = ["bucket"]
    search_fields = ["name"]

    @admin.display(description="Investors")
    def investor_count(self, obj):
        return obj.investors.count()


# ---------------------------------------------------------------------------
# Investors — including manual merge and edit
# ---------------------------------------------------------------------------

@admin.register(Investor)
class InvestorAdmin(admin.ModelAdmin):
    """Full record, including the relationship block.

    Relationship fields are editable here and only here — the pipeline never
    writes them, and the API only reads them for internal audiences.
    """

    list_display = ["name", "category", "hq_country", "overall_deals",
                    "avg_ticket_usd_mn", "has_relationship",
                    "classification_source"]
    list_filter = ["category", "classification_source", "is_active",
                   "hq_country"]
    search_fields = ["name", "normalised_name", "rel_contact_name"]
    readonly_fields = ["normalised_name", "overall_deals", "avg_ticket_usd_mn",
                       "last_deal_date", "last_deal_size_usd_mn",
                       "classified_at", "created_at", "updated_at"]
    actions = ["merge_selected", "reclassify_selected", "recompute_aggregates"]

    fieldsets = (
        ("Identity", {"fields": ["name", "normalised_name", "aliases",
                                 "is_active", "tenant_id"]}),
        ("Classification", {
            "fields": ["category", "hq_city", "hq_country", "has_local_office",
                       "single_sector_focused", "is_impact",
                       "classification_source", "classification_confidence",
                       "classification_model", "classified_at"],
            "description": ("Set by the LLM for new investors only. Editing "
                            "here marks the record as manually classified and "
                            "the pipeline will not revisit it.")}),
        ("Relationship (internal only)", {
            "fields": ["rel_internal_name", "rel_owner", "rel_met",
                       "rel_relationship", "rel_contact_name",
                       "rel_contact_title", "rel_mobile", "rel_emails",
                       "rel_past_outreach", "rel_past_revert", "rel_notes"],
            "description": ("Never written by the refresh pipeline and not "
                            "recoverable from any source. Shown to internal "
                            "users; never sent to a founder view or an "
                            "export.")}),
        ("Derived", {"fields": ["overall_deals", "avg_ticket_usd_mn",
                                "last_deal_date", "last_deal_size_usd_mn"]}),
    )

    @admin.display(boolean=True, description="Relationship")
    def has_relationship(self, obj):
        return bool(obj.rel_contact_name or obj.rel_relationship
                    or obj.rel_emails)

    def save_model(self, request, obj, form, change):
        if change and "category" in (form.changed_data or []):
            obj.classification_source = "manual"
        super().save_model(request, obj, form, change)

    @admin.action(description="Merge selected into the first (oldest) record")
    def merge_selected(self, request, queryset):
        """Manual merge — no LLM, no threshold, just a human decision."""
        from django.db import transaction
        from fundos.investors.pipeline import recompute_investor_aggregates

        rows = list(queryset.order_by("created_at"))
        if len(rows) < 2:
            self.message_user(request, "Select two or more investors to merge.",
                              level=messages.WARNING)
            return
        target, others = rows[0], rows[1:]

        with transaction.atomic():
            for dup in others:
                InvestorDeal.objects.filter(investor=dup).update(investor=target)
                for f in Investor.RELATIONSHIP_FIELDS:
                    incoming = getattr(dup, f, None)
                    if incoming and not getattr(target, f, None):
                        setattr(target, f, incoming)
                target.aliases = list(dict.fromkeys(
                    list(target.aliases or []) + [dup.name]
                    + list(dup.aliases or [])))
                dup.delete()
            target.save()
        recompute_investor_aggregates(investor_ids=[target.id])
        self.message_user(
            request,
            f"Merged {len(others)} record(s) into {target.name}. Deal rows "
            f"moved and aggregates recomputed.")

    @admin.action(description="Re-classify with the LLM")
    def reclassify_selected(self, request, queryset):
        from fundos.investors.classify import classify_investor
        cats = list(InvestorCategory.objects.select_related("bucket"))
        done = 0
        for inv in queryset:
            try:
                result = classify_investor(inv, cats)
            except Exception as e:
                self.message_user(request, f"{inv.name}: {e}",
                                  level=messages.ERROR)
                continue
            if result and result.get("category"):
                inv.category = result["category"]
                inv.hq_city = result.get("hq_city") or inv.hq_city
                inv.hq_country = result.get("hq_country") or inv.hq_country
                inv.classification_source = "llm"
                inv.classification_confidence = result.get("confidence")
                inv.save()
                done += 1
        self.message_user(request, f"{done} investor(s) re-classified.")

    @admin.action(description="Recompute deal counts and average ticket")
    def recompute_aggregates(self, request, queryset):
        from fundos.investors.pipeline import recompute_investor_aggregates
        n = recompute_investor_aggregates(
            investor_ids=list(queryset.values_list("id", flat=True)))
        self.message_user(request, f"{n} investor(s) recomputed.")


@admin.register(InvestorAliasCandidate)
class InvestorAliasCandidateAdmin(admin.ModelAdmin):
    """The below-90% identity queue.

    The LLM run is a MANUAL action here — never part of a refresh. An automatic
    merge on a machine judgement changes deal counts, deal counts drive the
    ranking, and nothing on screen would look wrong. So a human presses the
    button, reads the verdict, and still decides.
    """

    list_display = ["raw_name", "candidate", "fuzzy_score", "occurrences",
                    "llm_verdict", "llm_confidence", "status"]
    list_filter = ["status", "llm_verdict"]
    search_fields = ["raw_name", "candidate__name"]
    readonly_fields = ["raw_name", "candidate", "fuzzy_score", "alternatives",
                       "occurrences", "llm_verdict", "llm_confidence",
                       "llm_reasoning", "llm_sources", "llm_run_at",
                       "llm_run_by", "created_at"]
    actions = ["run_llm_adjudication", "merge_candidates",
               "keep_candidates_separate"]

    @admin.action(description="Ask the LLM (web search) — same entity or not?")
    def run_llm_adjudication(self, request, queryset):
        from fundos.investors import identity
        done, failed = 0, 0
        for cand in queryset.filter(status__in=["pending", "llm_done"]):
            try:
                identity.adjudicate(cand, user=request.user)
                done += 1
            except Exception as e:
                failed += 1
                logger.error("Adjudication failed for %s: %s", cand.id, e)
        msg = f"{done} adjudicated."
        if failed:
            msg += f" {failed} failed — see diagnostics."
        self.message_user(request, msg,
                          level=messages.WARNING if failed else messages.INFO)

    @admin.action(description="Merge into the candidate investor")
    def merge_candidates(self, request, queryset):
        from fundos.investors import identity
        n = 0
        for cand in queryset.exclude(status="merged"):
            try:
                identity.merge(cand, user=request.user)
                n += 1
            except Exception as e:
                self.message_user(request, f"{cand.raw_name}: {e}",
                                  level=messages.ERROR)
        self.message_user(request, f"{n} merged.")

    @admin.action(description="Keep separate")
    def keep_candidates_separate(self, request, queryset):
        from fundos.investors import identity
        for cand in queryset:
            identity.keep_separate(cand, user=request.user)
        self.message_user(request, f"{queryset.count()} kept separate.")


@admin.register(ClassificationReviewItem)
class ClassificationReviewAdmin(admin.ModelAdmin):
    list_display = ["investor", "proposed_category", "confidence", "status",
                    "created_at"]
    list_filter = ["status", "proposed_category"]
    search_fields = ["investor__name"]
    actions = ["accept_proposal"]

    @admin.action(description="Accept the proposed classification")
    def accept_proposal(self, request, queryset):
        n = 0
        for item in queryset.filter(status="open"):
            inv = item.investor
            inv.category = item.proposed_category
            inv.hq_city = item.proposed_hq_city or inv.hq_city
            inv.hq_country = item.proposed_hq_country or inv.hq_country
            inv.classification_source = "manual"
            inv.classification_confidence = item.confidence
            inv.save()
            item.status = "accepted"
            item.save(update_fields=["status"])
            n += 1
        self.message_user(request, f"{n} accepted.")


# ---------------------------------------------------------------------------
# Deal data
# ---------------------------------------------------------------------------

@admin.register(FundingDeal)
class FundingDealAdmin(admin.ModelAdmin):
    list_display = ["company_name", "deal_date", "amount_usd_mn", "sector",
                    "sub_sector", "round_type", "source"]
    list_filter = ["source", "sector", "sub_sector", "round_type"]
    search_fields = ["company_name", "raw_investors"]
    date_hierarchy = "deal_date"
    readonly_fields = ["dedup_key", "first_seen_run", "last_seen_run",
                       "created_at"]


@admin.register(InvestorDeal)
class InvestorDealAdmin(admin.ModelAdmin):
    list_display = ["investor", "company_name", "deal_date",
                    "deal_size_usd_mn", "sub_sector", "is_lead"]
    list_filter = ["is_lead", "sub_sector"]
    search_fields = ["investor__name", "company_name"]
    date_hierarchy = "deal_date"
    raw_id_fields = ["investor", "funding_deal"]


@admin.register(InvestorShortlist)
class InvestorShortlistAdmin(admin.ModelAdmin):
    list_display = ["deal", "investor", "match_score", "tier_at_save",
                    "saved_at"]
    search_fields = ["investor__name"]
    raw_id_fields = ["deal", "investor"]


# ---------------------------------------------------------------------------
# Matching configuration
# ---------------------------------------------------------------------------

@admin.register(RaiseBand)
class RaiseBandAdmin(admin.ModelAdmin):
    list_display = ["raise_size_usd_mn", "min_cheque_usd_mn",
                    "max_cheque_usd_mn", "notes"]
    ordering = ["raise_size_usd_mn"]

    def changelist_view(self, request, extra_context=None):
        extra_context = extra_context or {}
        extra_context["title"] = (
            "Raise bands — lookup is exact match or next smaller. Below the "
            "smallest row, the smallest row applies.")
        return super().changelist_view(request, extra_context)


@admin.register(MatchConfig)
class MatchConfigAdmin(admin.ModelAdmin):
    list_display = ["name", "is_active", "band_test_basis", "activity_weight",
                    "ticket_fit_weight", "updated_at", "preview_link"]
    readonly_fields = ["updated_at"]
    fieldsets = (
        (None, {"fields": ["name", "is_active"]}),
        ("Band test basis", {
            "fields": ["band_test_basis"],
            "description": (
                "Confirmed as DEAL SIZE. The source workbook tests the deal "
                "size in every band check and computes investor ATS without "
                "ever using it — deal size is what reproduces the published "
                "reference counts of 301 investors / 477 deals.")}),
        ("Ranking weights (tiers 3 and 4)", {
            "fields": ["activity_weight", "ticket_fit_weight",
                       "activity_constant"],
            "description": (
                "RelScore = 100 x (activity_weight x fA + ticket_fit_weight x "
                "fF), where fA = n/(n+k) saturates with deal count and fF is a "
                "scale-free penalty on distance from the band midpoint.")}),
        ("Exclusivity and splits", {
            "fields": ["exclusivity_threshold", "exclusivity_min_deals",
                       "pe_split_threshold"]}),
        ("Defaults", {"fields": ["default_window_months"]}),
    )

    @admin.display(description="Preview")
    def preview_link(self, obj):
        return format_html(
            '<a class="button" href="{}">Run preview</a>',
            reverse("admin:investors_matchconfig_preview"))

    def get_urls(self):
        return [path("preview/", self.admin_site.admin_view(self.preview_view),
                     name="investors_matchconfig_preview")] + super().get_urls()

    def preview_view(self, request):
        """Run the engine and compare against the workbook's own counts.

        This turns the reference figures into a permanent regression test an
        admin can run after any config change, rather than something that
        drifts unnoticed.
        """
        from fundos.investors import matching

        context = dict(self.admin_site.each_context(request),
                       title="Matching engine preview", result=None,
                       error=None)
        raise_size = request.GET.get("raise", "5")
        sub_name = request.GET.get("subSector", "D2C")
        date_from = request.GET.get("from", "2023-01-01")
        date_to = request.GET.get("to", "2026-06-05")

        try:
            import datetime as dt
            from decimal import Decimal
            sub = CanonicalSector.objects.filter(name__iexact=sub_name).first()
            q = matching.MatchQuery(
                raise_size_usd_mn=Decimal(raise_size),
                sub_sector_ids=[str(sub.id)] if sub else [],
                date_from=dt.date.fromisoformat(date_from),
                date_to=dt.date.fromisoformat(date_to))
            result = matching.run_match(q, limit=15)
            result["matchesReference"] = (
                result["totalInvestors"] == 301
                and result["totalMatchedDeals"] == 477)
            context["result"] = result
        except Exception as e:
            context["error"] = str(e)

        context.update({"raise_size": raise_size, "sub_name": sub_name,
                        "date_from": date_from, "date_to": date_to})
        return TemplateResponse(request, "admin/investors/match_preview.html",
                                context)


# ---------------------------------------------------------------------------
# Refresh
# ---------------------------------------------------------------------------

@admin.register(DataSource)
class DataSourceAdmin(admin.ModelAdmin):
    list_display = ["name", "adapter", "enabled", "articles_per_run",
                    "last_run_at", "last_run_status"]
    readonly_fields = ["last_run_at", "last_run_status"]
    fieldsets = (
        (None, {"fields": ["name", "adapter", "base_url", "enabled"]}),
        ("Scope", {"fields": ["articles_per_run", "manual_urls"],
                   "description": ("Manual URLs are read FIRST, before "
                                   "discovered ones — for reports published "
                                   "but not yet on the source's listing "
                                   "page.")}),
        ("Resilience", {"fields": ["request_delay_seconds", "max_retries",
                                   "breaker_failure_threshold",
                                   "breaker_recovery_seconds"]}),
        ("Runtime", {"fields": ["browser_path"]}),
    )


@admin.register(RefreshSchedule)
class RefreshScheduleAdmin(admin.ModelAdmin):
    list_display = ["__str__", "enabled", "source", "next_run_display",
                    "last_fired_at"]
    readonly_fields = ["last_fired_at", "next_run_display"]

    @admin.display(description="Next run")
    def next_run_display(self, obj):
        try:
            return obj.next_run().strftime("%a %d %b %Y, %H:%M %Z")
        except Exception:
            return "—"


@admin.register(RefreshRun)
class RefreshRunAdmin(admin.ModelAdmin):
    """The refresh console.

    Dry run, scoped run, stage isolation and commit are all here because the
    person who needs to verify a refresh before it lands is not necessarily
    the person with shell access.
    """

    list_display = ["started_at", "run_type", "source_name", "status",
                    "summary_display", "duration_display"]
    list_filter = ["run_type", "status"]
    readonly_fields = ["run_type", "source_name", "scope", "status",
                       "stage_reports", "summary", "triggered_by",
                       "started_at", "finished_at"]
    change_list_template = "admin/investors/refreshrun_changelist.html"

    @admin.display(description="Summary")
    def summary_display(self, obj):
        s = obj.summary or {}
        parts = [f"{k.replace('_', ' ')}: {v}"
                 for k, v in list(s.items())[:4]]
        return "; ".join(parts) or "—"

    @admin.display(description="Duration")
    def duration_display(self, obj):
        d = obj.duration_seconds
        return f"{d:.0f}s" if d else "—"

    def has_add_permission(self, request):
        return False

    def get_urls(self):
        return [
            path("console/", self.admin_site.admin_view(self.console_view),
                 name="investors_refresh_console"),
        ] + super().get_urls()

    def console_view(self, request):
        from fundos.investors.pipeline import run_refresh

        if request.method == "POST":
            stages = request.POST.getlist("stages") or None
            dry = request.POST.get("mode") != "commit"
            limit = request.POST.get("limitArticles") or None
            single = request.POST.get("singleUrl") or None
            try:
                run = run_refresh(
                    dry_run=dry, stages=stages,
                    limit_articles=int(limit) if limit else None,
                    single_url=single or None, user=request.user)
                self.message_user(
                    request,
                    f"{'Dry run' if dry else 'Refresh'} {run.status}: "
                    f"{run.summary}")
                return HttpResponseRedirect(
                    reverse("admin:investors_refreshrun_change",
                            args=[run.id]))
            except Exception as e:
                self.message_user(request, f"Refresh failed: {e}",
                                  level=messages.ERROR)

        schedule = RefreshSchedule.objects.filter(enabled=True).first()
        context = dict(
            self.admin_site.each_context(request),
            title="Data refresh console",
            sources=DataSource.objects.all(),
            schedule=schedule,
            next_run=(schedule.next_run() if schedule else None),
            recent=RefreshRun.objects.all()[:10],
            stages=["scrape", "explode", "enrich", "merge", "derive"])
        return TemplateResponse(request, "admin/investors/refresh_console.html",
                                context)
