"""
M3 Stage-3 Investment Materials models (Doc 2 M3.1–M3.8).

All rows here reference the strategy_profile_id in force at creation
(BR-M3-000 hard gate) — content stays traceable to the approved strategy.
"""
from django.conf import settings
from django.db import models

from fundos.core.models.base import DealScopedModel


class DealWorkspace(DealScopedModel):
    """M3.1 workspace — one per deal (Doc 2 M3.1)."""

    strategy_profile_id = models.UUIDField()
    status = models.CharField(max_length=24, default="active")

    class Meta:
        db_table = "deal_workspace"
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id"], condition=models.Q(is_deleted=False),
                name="uq_workspace_live"),
        ]


class AIContextSnapshot(models.Model):
    """Per-generation context snapshot (Doc 2 M3.1) — transparency/audit."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(db_index=True)
    workspace_id = models.UUIDField(null=True, blank=True)
    role = models.CharField(max_length=48)
    context_json = models.JSONField(default=dict)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "ai_context_snapshot"


class InvestmentStory(DealScopedModel):
    """M3.2 — the story arc; single source of narrative truth (BR-M3-020)."""

    STATUS = (("draft", "draft"), ("in_review", "in_review"),
              ("approved", "approved"))

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="stories")
    strategy_profile_id = models.UUIDField()
    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS, default="draft")
    thesis_primary = models.TextField(blank=True, default="")
    selected_positioning = models.TextField(blank=True, default="")

    class Meta:
        db_table = "investment_story"


class StoryComponent(DealScopedModel):
    """12 canonical components (Doc 2 M3.2)."""

    story = models.ForeignKey(InvestmentStory, on_delete=models.CASCADE,
                              related_name="components")
    component = models.CharField(max_length=32)   # vision…exit
    content = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=list, blank=True)
    edited_by_founder = models.BooleanField(default=False)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "story_component"
        ordering = ("sort_order",)


class StoryPositioningOption(DealScopedModel):
    story = models.ForeignKey(InvestmentStory, on_delete=models.CASCADE,
                              related_name="positioning_options")
    text = models.TextField()
    rationale = models.TextField(blank=True, default="")
    is_selected = models.BooleanField(default=False)

    class Meta:
        db_table = "story_positioning_option"


class MessageBlock(DealScopedModel):
    """Elevator / 30s / 2m / 5m / ask blocks (Doc 2 M3.2)."""

    story = models.ForeignKey(InvestmentStory, on_delete=models.CASCADE,
                              related_name="message_blocks")
    block_type = models.CharField(max_length=16)
    content = models.TextField(blank=True, default="")

    class Meta:
        db_table = "message_block"


class Teaser(DealScopedModel):
    """M3.3 — content rows; presentation via document_version (Doc 2 M3.3)."""

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="teasers")
    document_id = models.UUIDField(null=True, blank=True)
    strategy_profile_id = models.UUIDField()
    headline = models.TextField(blank=True, default="")
    thesis = models.TextField(blank=True, default="")
    tagline = models.TextField(blank=True, default="")

    class Meta:
        db_table = "teaser"


class TeaserSection(DealScopedModel):
    teaser = models.ForeignKey(Teaser, on_delete=models.CASCADE,
                               related_name="sections")
    section_key = models.CharField(max_length=48)
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    is_mandatory = models.BooleanField(default=False)
    included = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "teaser_section"
        ordering = ("sort_order",)


class PitchDeck(DealScopedModel):
    """M3.4 — story-first two-step generation (Doc 2 M3.4)."""

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="decks")
    document_id = models.UUIDField(null=True, blank=True)
    strategy_profile_id = models.UUIDField()
    narrative_outline = models.JSONField(default=list, blank=True)  # step 1
    outline_approved = models.BooleanField(default=False)

    class Meta:
        db_table = "pitch_deck"


class Slide(DealScopedModel):
    deck = models.ForeignKey(PitchDeck, on_delete=models.CASCADE,
                             related_name="slides")
    slide_type = models.CharField(max_length=32)
    headline = models.TextField(blank=True, default="")
    narrative = models.TextField(blank=True, default="")
    key_metrics = models.JSONField(default=list, blank=True)
    chart_spec = models.JSONField(null=True, blank=True)
    icons = models.JSONField(default=list, blank=True)
    speaker_notes = models.TextField(blank=True, default="")
    investor_takeaway = models.TextField(blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "slide"
        ordering = ("sort_order",)


class FinancialModel(DealScopedModel):
    """M3.5 — assumptions live separately from statements; statements are
    recomputed [DET] from assumptions (BR-M3-050)."""

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="financial_models")
    document_id = models.UUIDField(null=True, blank=True)
    strategy_profile_id = models.UUIDField()
    horizon_months = models.IntegerField(default=36)
    status = models.CharField(max_length=24, default="draft")

    class Meta:
        db_table = "financial_model"


class FinancialAssumption(DealScopedModel):
    SOURCE = (("founder", "founder"), ("ai_default_peer", "ai_default_peer"),
              ("import", "import"))

    model = models.ForeignKey(FinancialModel, on_delete=models.CASCADE,
                              related_name="assumptions")
    group = models.CharField(max_length=32)     # revenue/hiring/opex/capex/wc
    key = models.CharField(max_length=64)
    value_num = models.DecimalField(max_digits=20, decimal_places=4,
                                    null=True, blank=True)
    unit = models.CharField(max_length=16, blank=True, default="")
    source = models.CharField(max_length=24, choices=SOURCE,
                              default="ai_default_peer")

    class Meta:
        db_table = "financial_assumption"
        unique_together = [("model", "group", "key")]


class FinancialStatementLine(models.Model):
    """[DET] computed monthly statement lines — high row volume."""

    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(db_index=True)
    model = models.ForeignKey(FinancialModel, on_delete=models.CASCADE,
                              related_name="statement_lines")
    scenario = models.CharField(max_length=16, default="base")
    statement = models.CharField(max_length=8)   # pl | bs | cf
    line_key = models.CharField(max_length=48)   # revenue, cogs, opex, cash…
    month_index = models.IntegerField()          # 1..horizon
    value = models.DecimalField(max_digits=20, decimal_places=2)
    ccy = models.CharField(max_length=3, default="INR")

    class Meta:
        db_table = "financial_statement_line"
        indexes = [models.Index(fields=["model", "scenario", "statement"])]


class FinancialKpi(models.Model):
    id = models.BigAutoField(primary_key=True)
    tenant_id = models.UUIDField(db_index=True)
    deal_id = models.UUIDField(db_index=True)
    model = models.ForeignKey(FinancialModel, on_delete=models.CASCADE,
                              related_name="kpis")
    scenario = models.CharField(max_length=16, default="base")
    kpi_key = models.CharField(max_length=48)    # arr, burn, runway, ltv_cac…
    month_index = models.IntegerField()
    value = models.DecimalField(max_digits=20, decimal_places=4)

    class Meta:
        db_table = "financial_kpi"


class FinancialScenario(DealScopedModel):
    model = models.ForeignKey(FinancialModel, on_delete=models.CASCADE,
                              related_name="scenarios")
    name = models.CharField(max_length=16)       # base/upside/downside
    adjustments = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "financial_scenario"
        unique_together = [("model", "name")]


class FinancialImport(DealScopedModel):
    """Founder-uploaded actuals mapping (Doc 2 M3.5)."""

    model = models.ForeignKey(FinancialModel, on_delete=models.CASCADE,
                              related_name="imports")
    material_asset_id = models.UUIDField(null=True, blank=True)
    mapping = models.JSONField(default=dict, blank=True)
    status = models.CharField(max_length=16, default="pending")

    class Meta:
        db_table = "financial_import"


class InvestmentMemorandum(DealScopedModel):
    """M3.6 — 16 mandatory sections (Doc 2 M3.6)."""

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="ims")
    document_id = models.UUIDField(null=True, blank=True)
    strategy_profile_id = models.UUIDField()

    class Meta:
        db_table = "investment_memorandum"


class ImSection(DealScopedModel):
    im = models.ForeignKey(InvestmentMemorandum, on_delete=models.CASCADE,
                           related_name="sections")
    section_key = models.CharField(max_length=48)
    title = models.CharField(max_length=255, blank=True, default="")
    body = models.TextField(blank=True, default="")
    is_mandatory = models.BooleanField(default=True)
    consistency_flags = models.JSONField(default=list, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "im_section"
        ordering = ("sort_order",)


class ImChart(DealScopedModel):
    im = models.ForeignKey(InvestmentMemorandum, on_delete=models.CASCADE,
                           related_name="charts")
    section_key = models.CharField(max_length=48, blank=True, default="")
    chart_spec = models.JSONField(default=dict)

    class Meta:
        db_table = "im_chart"


class ImAppendix(DealScopedModel):
    im = models.ForeignKey(InvestmentMemorandum, on_delete=models.CASCADE,
                           related_name="appendices")
    title = models.CharField(max_length=255)
    document_id = models.UUIDField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "im_appendix"
        ordering = ("sort_order",)


class PackageReview(DealScopedModel):
    """M3.7 — six-category quality review (Doc 2 M3.7)."""

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="reviews")
    strategy_profile_id = models.UUIDField()
    category_notes = models.JSONField(default=dict, blank=True)
    risk_summary = models.JSONField(default=list, blank=True)
    outstanding_actions = models.JSONField(default=list, blank=True)
    scoring_config_version_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "package_review"


class ReviewFinding(DealScopedModel):
    review = models.ForeignKey(PackageReview, on_delete=models.CASCADE,
                               related_name="findings")
    priority = models.CharField(max_length=8, default="med")    # high/med/low
    category = models.CharField(max_length=24, blank=True, default="")
    reason = models.TextField(blank=True, default="")
    business_impact = models.TextField(blank=True, default="")
    affected_documents = models.JSONField(default=list, blank=True)
    estimated_improvement = models.CharField(max_length=128, blank=True,
                                             default="")
    status = models.CharField(max_length=16, default="open")   # open/resolved/dismissed

    class Meta:
        db_table = "review_finding"


class CrossDocInconsistency(DealScopedModel):
    """[DET] numeric cross-document validation output (Doc 2 M3.7)."""

    review = models.ForeignKey(PackageReview, on_delete=models.CASCADE,
                               related_name="inconsistencies")
    field_key = models.CharField(max_length=64)
    values = models.JSONField(default=list)     # [{document, value}]
    recommended_correction = models.TextField(blank=True, default="")
    resolved = models.BooleanField(default=False)

    class Meta:
        db_table = "crossdoc_inconsistency"


class InvestorObjection(DealScopedModel):
    """Objection simulation rows (Doc 2 M3.7)."""

    review = models.ForeignKey(PackageReview, on_delete=models.CASCADE,
                               related_name="objections")
    investor_type = models.CharField(max_length=32, blank=True, default="")
    question = models.TextField()
    reason = models.TextField(blank=True, default="")
    supporting_data = models.JSONField(default=dict, blank=True)
    suggested_response = models.TextField(blank=True, default="")
    evidence = models.JSONField(default=list, blank=True)
    confidence = models.CharField(max_length=8, blank=True, default="")
    priority = models.CharField(max_length=8, blank=True, default="")

    class Meta:
        db_table = "investor_objection"


class InvestorPackage(DealScopedModel):
    """M3.8 — the approved frozen baseline (BR-M3-080)."""

    STATUS = (("draft", "draft"), ("approved", "approved"))

    workspace = models.ForeignKey(DealWorkspace, on_delete=models.CASCADE,
                                  related_name="packages")
    strategy_profile_id = models.UUIDField()
    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS, default="draft")
    document_version_ids = models.JSONField(default=list, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                    blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    approved_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "investor_package"
