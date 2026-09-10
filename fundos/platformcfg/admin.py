"""
Django Admin — the single place an administrator configures runtime
behaviour. Nothing here is exposed as end-user UI.

Everything registered below changes what users experience without a code
change or a release: AI prompts, branding, the stage journey, fund-raise
buckets and the traction rules that pick one, profile sections and their
completeness rules, countries, FX rates and editable UI copy.
"""
from django.contrib import admin
from django.utils.html import format_html

from fundos.platformcfg.models import (
    BrandConfig, Country, FundraiseBucket, FxRate, ProfileSectionConfig,
    PromptTemplate, ResearchQuestion, ResearchQuestionBatch, StageDefinition,
    TractionRule, UiCopy, UpliftRule,
)


@admin.register(PromptTemplate)
class PromptTemplateAdmin(admin.ModelAdmin):
    list_display = ("role", "tier_label", "version_no", "is_active", "preview", "updated_at")
    list_filter = ("is_active", "tier", "role")
    search_fields = ("role", "system_prompt", "notes")
    readonly_fields = ("updated_at", "available_context")
    fieldsets = (
        (None, {
            "fields": ("role", "purpose", "tier", "is_active", "version_no"),
            "description": (
                "Edit the prompt sent to the language model for this role. "
                "Changes take effect on the next generation — no restart. "
                "Deactivate a row to fall back to the default shipped in code. "
                "TIER decides whether this prompt runs on the Simple LLM "
                "(cheap, no web search — for reading/Q&A over data already "
                "retrieved) or the Advanced LLM (web search + complex "
                "generation — used for the exhaustive Company Profile). The "
                "actual model behind each tier is set per tenant under "
                "LLM → Tenant LLM tiers."
            ),
        }),
        ("Prompt", {
            "fields": ("system_prompt", "user_prompt"),
            "description": (
                "IMPORTANT: never ask the model to produce or recalculate "
                "numbers. All figures are computed by the deterministic engines "
                "and injected as read-only context; response schemas reject "
                "numeric authority fields, so a prompt that requests them will "
                "fail validation."
            ),
        }),
        ("Reference", {
            "fields": ("available_context", "context_keys", "notes", "updated_at"),
        }),
    )

    @admin.display(description="Tier")
    def tier_label(self, obj):
        if obj.tier:
            return obj.tier
        try:
            from fundos.llm.default_prompts import get_tier
            return f"{get_tier(obj.role)} (default)"
        except Exception:
            return "—"

    def preview(self, obj):
        text = (obj.system_prompt or "")[:80]
        return f"{text}…" if len(obj.system_prompt or "") > 80 else text
    preview.short_description = "System prompt"

    def available_context(self, obj):
        """Show the admin which context keys this role actually receives."""
        from fundos.llm.default_prompts import get_default
        default = get_default(obj.role) if obj and obj.role else None
        if not default:
            return "—"
        keys = default.get("context_keys", [])
        note = default.get("notes", "")
        html = "<b>Context keys available to this role:</b><br>"
        html += "<code>" + ", ".join(keys) + "</code>"
        if note:
            html += f"<br><br><b>Note:</b> {note}"
        return format_html(html)
    available_context.short_description = "Available context"

    actions = ["reset_to_default"]

    @admin.action(description="Reset selected prompts to shipped defaults")
    def reset_to_default(self, request, queryset):
        from fundos.llm.default_prompts import get_default
        count = 0
        for row in queryset:
            default = get_default(row.role)
            if default:
                row.system_prompt = default["system"]
                row.user_prompt = default.get("user", "")
                row.context_keys = default.get("context_keys", [])
                row.purpose = default.get("notes", "") or row.purpose
                if not row.tier and default.get("tier"):
                    row.tier = default["tier"]
                row.version_no += 1
                row.save()
                count += 1
        self.message_user(request, f"{count} prompt(s) reset to default.")


@admin.register(BrandConfig)
class BrandConfigAdmin(admin.ModelAdmin):
    list_display = ("product_name", "browser_title", "updated_at")
    fieldsets = (
        ("Identity", {
            "fields": ("product_name", "legal_name", "tagline", "browser_title"),
            "description": (
                "Renaming the product here changes it across the app, generated "
                "documents and notifications. Internal identifiers and database "
                "tables are deliberately unaffected."
            ),
        }),
        ("Assets", {
            "fields": ("logo_url", "logo_mono_url", "favicon_url",
                       "export_logo_url", "primary_color"),
        }),
        ("Documents & contact", {
            "fields": ("export_footer_text", "support_email"),
        }),
    )

    def has_add_permission(self, request):
        return not BrandConfig.objects.exists()


@admin.register(Country)
class CountryAdmin(admin.ModelAdmin):
    list_display = ("name", "iso2", "iso3", "dial_code", "home_currency",
                    "sort_order", "is_active")
    list_display_links = ("name",)
    list_filter = ("is_active", "home_currency")
    search_fields = ("name", "iso2", "iso3")
    list_editable = ("sort_order", "is_active", "home_currency")
    ordering = ("sort_order", "name")


@admin.register(FundraiseBucket)
class FundraiseBucketAdmin(admin.ModelAdmin):
    list_display = ("bucket_no", "label", "amount_usd", "typical_stage",
                    "dilution_range", "is_active")
    list_display_links = ("label",)
    list_editable = ("is_active",)
    ordering = ("bucket_no",)
    fieldsets = (
        (None, {
            "fields": ("bucket_no", "label", "amount_usd", "typical_stage",
                       "is_active"),
            "description": (
                "The seven fund-raise values. These are the amounts a founder "
                "can target. The traction rules below select which one is "
                "recommended by default; the founder may choose any other."
            ),
        }),
        ("Investor profile & dilution", {
            "fields": ("investor_types", "typical_dilution_low_pct",
                       "typical_dilution_high_pct", "description"),
        }),
    )

    def dilution_range(self, obj):
        return f"{obj.typical_dilution_low_pct}–{obj.typical_dilution_high_pct}%"
    dilution_range.short_description = "Typical dilution"


@admin.register(TractionRule)
class TractionRuleAdmin(admin.ModelAdmin):
    list_display = ("priority", "name", "recommended_bucket", "is_active")
    list_display_links = ("name",)
    list_editable = ("priority", "is_active")
    ordering = ("priority",)
    fieldsets = (
        (None, {
            "fields": ("name", "priority", "is_active", "recommended_bucket"),
            "description": (
                "Rules are evaluated in priority order (lowest first); the first "
                "rule whose conditions all match supplies the recommended bucket. "
                "Put the most specific rules at the lowest priority numbers."
            ),
        }),
        ("Conditions", {
            "fields": ("conditions",),
            "description": (
                "JSON object. Supported keys — all optional:<br>"
                "<code>arr_usd_min</code>, <code>arr_usd_max</code>, "
                "<code>capital_raised_usd_min</code>, "
                "<code>capital_raised_usd_max</code>, "
                "<code>paying_customers_min</code>, "
                "<code>founder_full_time</code> (true/false), "
                "<code>company_registered</code> (true/false), "
                "<code>months_full_time_min</code><br><br>"
                'Example: <code>{"arr_usd_min": 500000, "founder_full_time": true}</code>'
            ),
        }),
        ("Explanation shown to the founder", {
            "fields": ("rationale", "advice_note"),
        }),
    )


@admin.register(UpliftRule)
class UpliftRuleAdmin(admin.ModelAdmin):
    list_display = ("name", "condition_key", "levels", "is_active")
    list_display_links = ("name",)
    list_editable = ("levels", "is_active")


@admin.register(ProfileSectionConfig)
class ProfileSectionConfigAdmin(admin.ModelAdmin):
    list_display = ("sort_order", "label", "section_key", "kind",
                    "required_for_creation", "required_for_completeness",
                    "is_active")
    list_display_links = ("label",)
    list_editable = ("sort_order", "required_for_creation",
                     "required_for_completeness", "is_active")
    list_filter = ("kind", "is_active", "required_for_completeness")
    ordering = ("sort_order",)
    fieldsets = (
        (None, {
            "fields": ("section_key", "label", "description", "kind",
                       "sort_order", "is_active"),
        }),
        ("Behaviour", {
            "fields": ("llm_role", "is_editable", "is_regenerable"),
            "description": "The LLM role used when a founder clicks Regenerate.",
        }),
        ("Completeness rules", {
            "fields": ("required_for_creation", "required_for_completeness"),
            "description": (
                "<b>Required for creation</b> — must be present before a profile "
                "can be generated at all.<br>"
                "<b>Required for completeness</b> — counts toward the 'complete' "
                "state. A complete profile plus an owner's confirmation of review "
                "is what routes the dashboard to the last active stage rather "
                "than back to Stage 1. These two sets are deliberately different."
            ),
        }),
        ("Generated output contract", {
            "fields": ("container_kind", "spec_ref", "field_spec",
                       "storage_key"),
            "description": (
                "What the AI is asked to produce for this section, and what "
                "shape the answer must come back in.<br><br>"
                "<b>Field spec</b> is a JSON object of "
                "<code>{\"field_name\": \"description\"}</code>. Every entry "
                "is rendered into the generation prompt exactly as written, "
                "so the description is an INSTRUCTION to the model — "
                "<code>\"string - ISO-2 country code of headquarters, e.g. "
                "IN\"</code> works, <code>\"country\"</code> does not. Add a "
                "field here and the next run will ask for it and store it; "
                "no release needed.<br><br>"
                "<b>Container kind</b> must match what the fields describe: "
                "one record (object) or a list of them (array). Changing it "
                "on a populated section will not migrate existing data.<br>"
                "<b>Storage key</b> is internal. Leave it alone unless you "
                "know why it differs from the section key — changing it on a "
                "live install orphans everything already stored."
            ),
        }),
    )


class ResearchQuestionInline(admin.TabularInline):
    """The questions themselves, edited in the batch they belong to.

    Inline because a question only means something in the context of its
    batch: the batch is one LLM call, and what makes a good batch is ten
    questions a single search-grounded call can chase coherently.
    """

    model = ResearchQuestion
    extra = 1
    fields = ("sort_order", "text", "is_active")
    ordering = ("sort_order",)


@admin.register(ResearchQuestionBatch)
class ResearchQuestionBatchAdmin(admin.ModelAdmin):
    """The questions the system asks the web about a company.

    This is the research methodology, editable. One batch is one
    search-grounded LLM call, which makes the batch both the unit of cost and
    the unit of partial failure: switch one off and that call is not made and
    those sections go thin; a batch that fails at run time reports exactly
    which of its questions went unanswered.
    """

    list_display = ("sort_order", "topic", "code", "question_count", "covers",
                    "is_active")
    list_display_links = ("topic",)
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active",)
    search_fields = ("code", "topic", "covers", "questions__text")
    ordering = ("sort_order",)
    inlines = [ResearchQuestionInline]

    fieldsets = (
        (None, {
            "fields": ("code", "topic", "covers", "sort_order", "is_active"),
            "description": (
                "Each ACTIVE batch is one billed, search-grounded call per "
                "company profile generated. Deactivating batches is the most "
                "direct way to cut the cost of a run — at the price of "
                "leaving the sections named in <b>Covers</b> thinner."
            ),
        }),
    )

    @admin.display(description="Questions")
    def question_count(self, obj):
        active = obj.questions.filter(is_active=True).count()
        total = obj.questions.count()
        return f"{active} active / {total}"


@admin.register(ResearchQuestion)
class ResearchQuestionAdmin(admin.ModelAdmin):
    """Registered separately as well as inline, so questions are searchable
    across every batch — "which batch asks about churn?" is a question an
    analyst curating the bank will have."""

    list_display = ("batch", "sort_order", "short_text", "is_active")
    list_display_links = ("short_text",)
    list_editable = ("sort_order", "is_active")
    list_filter = ("is_active", "batch")
    search_fields = ("text",)
    ordering = ("batch__sort_order", "sort_order")

    @admin.display(description="Question")
    def short_text(self, obj):
        return obj.text[:110] + ("…" if len(obj.text) > 110 else "")


@admin.register(StageDefinition)
class StageDefinitionAdmin(admin.ModelAdmin):
    list_display = ("stage_no", "label", "stage_key", "is_implemented", "is_active")
    list_display_links = ("label",)
    list_editable = ("is_implemented", "is_active")
    ordering = ("stage_no",)
    fieldsets = (
        (None, {
            "fields": ("stage_no", "stage_key", "label", "is_implemented",
                       "is_active"),
            "description": (
                "The journey shown in the sidebar. This is the single source of "
                "truth for stage numbering and labels — the frontend reads it "
                "from the API rather than hard-coding them."
            ),
        }),
        ("Guidance shown on the stage page", {
            "fields": ("purpose", "overview", "next_actions"),
        }),
        ("Prerequisites", {
            "fields": ("prerequisites",),
            "description": (
                "<b>Advisory only.</b> Stages are never locked — a user can open "
                "any stage at any time. Prerequisites are displayed as met or "
                "pending so the user knows what is outstanding, and only the "
                "specific actions that depend on missing data are disabled.<br><br>"
                'Format: <code>[{"key": "...", "label": "...", '
                '"explanation": "..."}]</code>'
            ),
        }),
    )


@admin.register(FxRate)
class FxRateAdmin(admin.ModelAdmin):
    list_display = ("base_ccy", "quote_ccy", "rate", "as_of_date", "source")
    list_filter = ("base_ccy", "quote_ccy", "source")
    date_hierarchy = "as_of_date"
    ordering = ("-as_of_date",)


@admin.register(UiCopy)
class UiCopyAdmin(admin.ModelAdmin):
    list_display = ("key", "short_text", "description", "is_active")
    list_filter = ("is_active",)
    search_fields = ("key", "text", "description")

    def short_text(self, obj):
        return (obj.text[:70] + "…") if len(obj.text) > 70 else obj.text
    short_text.short_description = "Text"


admin.site.site_header = "Silk — Platform Administration"
admin.site.site_title = "Silk Admin"
admin.site.index_title = "Configuration"
