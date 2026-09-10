"""
M2 Stage-2 Fundraising Strategy models (Doc 2 M2.1–M2.7).
Money triples declared explicitly per Doc 2 GC-06.
"""
from django.conf import settings
from django.db import models

from fundos.core.models.base import DealScopedModel


class FounderObjectives(DealScopedModel):
    """M2.1 — versioned; change invalidates downstream (BR-M2-002)."""

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    purposes = models.JSONField(default=list)              # string[]
    timeline = models.CharField(max_length=16)             # FK→m_timeline
    runway = models.CharField(max_length=16)               # FK→m_runway_band
    preferred_investor_types = models.JSONField(default=list, blank=True)
    geographies = models.JSONField(default=list, blank=True)
    max_dilution_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                           null=True, blank=True)

    class Meta:
        db_table = "founder_objectives"


class PeerUniverse(DealScopedModel):
    """M2.2 — versioned; the approved reference dataset."""

    STATUS = (("draft", "draft"), ("approved", "approved"))

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS, default="draft")
    generated_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "peer_universe"


class PeerCompany(DealScopedModel):
    GROUPS = (("A", "A"), ("B", "B"), ("C", "C"))
    ORIGIN = (("ai_recommended", "ai_recommended"),
              ("founder_added", "founder_added"))

    peer_universe = models.ForeignKey(PeerUniverse, on_delete=models.CASCADE,
                                      related_name="peers")
    group_code = models.CharField(max_length=1, choices=GROUPS, default="A")
    name = models.CharField(max_length=255)
    logo_uri = models.CharField(max_length=512, blank=True, default="")
    description = models.TextField(blank=True, default="")
    sector = models.CharField(max_length=128, blank=True, default="")
    subsector = models.CharField(max_length=128, blank=True, default="")
    stage = models.CharField(max_length=64, blank=True, default="")
    geography = models.CharField(max_length=128, blank=True, default="")
    last_funding_round = models.CharField(max_length=64, blank=True, default="")
    current_status = models.CharField(max_length=64, blank=True, default="")
    similarity_score = models.DecimalField(max_digits=5, decimal_places=2,
                                           null=True, blank=True)
    similarity_breakdown = models.JSONField(default=dict, blank=True)  # BR-S2-010
    selection_rationale = models.TextField(blank=True, default="")
    origin = models.CharField(max_length=16, choices=ORIGIN,
                              default="ai_recommended")
    source_market_deal_ids = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "peer_company"
        indexes = [models.Index(fields=["peer_universe", "group_code"])]


class PeerMilestone(DealScopedModel):
    """Company Journey Timeline rows (Doc 2 M2.2)."""

    peer_company = models.ForeignKey(PeerCompany, on_delete=models.CASCADE,
                                     related_name="milestones")
    round = models.CharField(max_length=64, blank=True, default="")
    event_date = models.DateField(null=True, blank=True)
    amount_raised_value = models.DecimalField(max_digits=20, decimal_places=2,
                                              null=True, blank=True)
    amount_raised_ccy = models.CharField(max_length=3, blank=True, default="USD")
    amount_raised_basis = models.CharField(max_length=32, blank=True,
                                           default="actual")
    valuation_value = models.DecimalField(max_digits=20, decimal_places=2,
                                          null=True, blank=True)
    valuation_ccy = models.CharField(max_length=3, blank=True, default="USD")
    valuation_basis = models.CharField(max_length=32, blank=True,
                                       default="post_money")
    arr = models.DecimalField(max_digits=20, decimal_places=2, null=True,
                              blank=True)
    revenue = models.DecimalField(max_digits=20, decimal_places=2, null=True,
                                  blank=True)
    employees = models.IntegerField(null=True, blank=True)
    lead_investor = models.CharField(max_length=255, blank=True, default="")
    notable_milestones = models.TextField(blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "peer_milestone"
        ordering = ("sort_order",)


class PeerInsight(DealScopedModel):
    """AI insights, evidence-backed (Doc 2 M2.2 / LLM-M2-011)."""

    KIND = (("fact", "fact"), ("inference", "inference"))

    peer_universe = models.ForeignKey(PeerUniverse, on_delete=models.CASCADE,
                                      related_name="insights")
    peer_company = models.ForeignKey(PeerCompany, null=True, blank=True,
                                     on_delete=models.CASCADE, related_name="insights")
    text = models.TextField()
    kind = models.CharField(max_length=12, choices=KIND, default="inference")
    evidence = models.JSONField(default=list, blank=True)

    class Meta:
        db_table = "peer_insight"


class RaiseRecommendation(DealScopedModel):
    """M2.3 — versioned (Doc 2 M2.3)."""

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    recommended_value = models.DecimalField(max_digits=20, decimal_places=2,
                                            null=True, blank=True)
    recommended_ccy = models.CharField(max_length=3, default="USD")
    recommended_basis = models.CharField(max_length=32, default="recommendation")
    low_value = models.DecimalField(max_digits=20, decimal_places=2,
                                    null=True, blank=True)
    low_ccy = models.CharField(max_length=3, default="USD")
    low_basis = models.CharField(max_length=32, default="recommendation")
    high_value = models.DecimalField(max_digits=20, decimal_places=2,
                                     null=True, blank=True)
    high_ccy = models.CharField(max_length=3, default="USD")
    high_basis = models.CharField(max_length=32, default="recommendation")
    confidence = models.CharField(max_length=12, blank=True, default="")
    dilution_low_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                           null=True, blank=True)
    dilution_high_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                            null=True, blank=True)
    recommended_timeline = models.CharField(max_length=32, blank=True, default="")
    recommended_investor_profile = models.JSONField(default=dict, blank=True)
    funding_band = models.CharField(max_length=32, blank=True, default="")
    dimension_scores = models.JSONField(default=dict, blank=True)
    reasoning = models.JSONField(default=list, blank=True)
    risks = models.JSONField(default=list, blank=True)
    narrative = models.TextField(blank=True, default="")  # AI Generated Insights
    scoring_config_version_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "raise_recommendation"


class RaiseScenario(DealScopedModel):
    """≥3: Conservative/Recommended/Aggressive (Doc 2 M2.3)."""

    raise_recommendation = models.ForeignKey(
        RaiseRecommendation, on_delete=models.CASCADE, related_name="scenarios")
    label = models.CharField(max_length=32)
    raise_value = models.DecimalField(max_digits=20, decimal_places=2,
                                      null=True, blank=True)
    raise_ccy = models.CharField(max_length=3, default="USD")
    raise_basis = models.CharField(max_length=32, default="recommendation")
    dilution_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                       null=True, blank=True)
    runway_months = models.IntegerField(null=True, blank=True)
    assessment = models.TextField(blank=True, default="")
    is_selected = models.BooleanField(default=False)

    class Meta:
        db_table = "raise_scenario"


class Valuation(DealScopedModel):
    """M2.4 — versioned (Doc 2 M2.4)."""

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    range_low_value = models.DecimalField(max_digits=20, decimal_places=2,
                                          null=True, blank=True)
    range_low_ccy = models.CharField(max_length=3, default="USD")
    range_low_basis = models.CharField(max_length=32, default="indicative")
    range_high_value = models.DecimalField(max_digits=20, decimal_places=2,
                                           null=True, blank=True)
    range_high_ccy = models.CharField(max_length=3, default="USD")
    range_high_basis = models.CharField(max_length=32, default="indicative")
    confidence = models.CharField(max_length=12, blank=True, default="")
    football_field = models.JSONField(default=list, blank=True)
    assumptions = models.JSONField(default=list, blank=True)
    limitations = models.JSONField(default=list, blank=True)
    positioning_statement = models.TextField(blank=True, default="")
    scoring_config_version_id = models.UUIDField(null=True, blank=True)

    class Meta:
        db_table = "valuation"


class ValuationMethodResult(DealScopedModel):
    valuation = models.ForeignKey(Valuation, on_delete=models.CASCADE,
                                  related_name="method_results")
    method = models.CharField(max_length=48)
    low_value = models.DecimalField(max_digits=20, decimal_places=2,
                                    null=True, blank=True)
    low_ccy = models.CharField(max_length=3, default="USD")
    low_basis = models.CharField(max_length=32, default="indicative")
    mid_value = models.DecimalField(max_digits=20, decimal_places=2,
                                    null=True, blank=True)
    mid_ccy = models.CharField(max_length=3, default="USD")
    mid_basis = models.CharField(max_length=32, default="indicative")
    high_value = models.DecimalField(max_digits=20, decimal_places=2,
                                     null=True, blank=True)
    high_ccy = models.CharField(max_length=3, default="USD")
    high_basis = models.CharField(max_length=32, default="indicative")
    weight = models.DecimalField(max_digits=4, decimal_places=3, default=0)
    is_outlier = models.BooleanField(default=False)
    outlier_reason = models.TextField(blank=True, default="")
    assumptions = models.JSONField(default=list, blank=True)
    applicable = models.BooleanField(default=True)

    class Meta:
        db_table = "valuation_method_result"


class ValuationDilutionMatrix(DealScopedModel):
    valuation = models.ForeignKey(Valuation, on_delete=models.CASCADE,
                                  related_name="dilution_grid")
    raise_value = models.DecimalField(max_digits=20, decimal_places=2)
    raise_ccy = models.CharField(max_length=3, default="USD")
    raise_basis = models.CharField(max_length=32, default="scenario")
    valuation_value = models.DecimalField(max_digits=20, decimal_places=2)
    valuation_ccy = models.CharField(max_length=3, default="USD")
    valuation_basis = models.CharField(max_length=32, default="pre_money")
    dilution_pct = models.DecimalField(max_digits=5, decimal_places=2)
    founder_ownership_pct = models.DecimalField(max_digits=5, decimal_places=2)
    investor_ownership_pct = models.DecimalField(max_digits=5, decimal_places=2)
    esop_impact_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                          default=0)
    existing_investor_dilution_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=0)

    class Meta:
        db_table = "valuation_dilution_matrix"


class ValuationNegotiation(DealScopedModel):
    """Guardrailed guidance (Doc 2 M2.4 §7.10)."""

    KIND = (("supporting_argument", "supporting_argument"),
            ("investor_objection", "investor_objection"),
            ("suggested_response", "suggested_response"))

    valuation = models.ForeignKey(Valuation, on_delete=models.CASCADE,
                                  related_name="negotiation_items")
    kind = models.CharField(max_length=24, choices=KIND)
    text = models.TextField()
    evidence = models.JSONField(default=list, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "valuation_negotiation"
        ordering = ("sort_order",)


class InstrumentOption(DealScopedModel):
    """M2.5 — compared options; never prescribed (BR-M2-050)."""

    instrument = models.CharField(max_length=32)   # FK→m_instrument
    pros = models.JSONField(default=list, blank=True)
    cons = models.JSONField(default=list, blank=True)
    mechanics_explainer = models.TextField(blank=True, default="")
    market_context = models.TextField(blank=True, default="")
    applicability_note = models.TextField(blank=True, default="")
    is_selected = models.BooleanField(default=False)   # informational only

    class Meta:
        db_table = "instrument_option"


class DilutionScenario(DealScopedModel):
    """M2.5 — deterministic cap-table projections; ranges."""

    instrument = models.CharField(max_length=32)
    raise_value = models.DecimalField(max_digits=20, decimal_places=2)
    raise_ccy = models.CharField(max_length=3, default="USD")
    raise_basis = models.CharField(max_length=32, default="scenario")
    valuation_low_value = models.DecimalField(max_digits=20, decimal_places=2)
    valuation_low_ccy = models.CharField(max_length=3, default="USD")
    valuation_low_basis = models.CharField(max_length=32, default="pre_money")
    valuation_high_value = models.DecimalField(max_digits=20, decimal_places=2)
    valuation_high_ccy = models.CharField(max_length=3, default="USD")
    valuation_high_basis = models.CharField(max_length=32, default="pre_money")
    pre_money = models.JSONField(default=dict, blank=True)
    post_money = models.JSONField(default=dict, blank=True)
    ownership_bands = models.JSONField(default=dict, blank=True)
    conversion_assumptions = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "dilution_scenario"


class FundraisingStrategy(DealScopedModel):
    """M2.6 — versioned assembled blueprint (Doc 2 M2.6)."""

    STATUS = (("draft", "draft"), ("approved", "approved"))

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    executive_summary = models.TextField(blank=True, default="")
    current_position = models.JSONField(default=dict, blank=True)
    recommended_raise = models.JSONField(default=dict, blank=True)
    recommended_valuation = models.JSONField(default=dict, blank=True)
    why_this_strategy = models.TextField(blank=True, default="")
    risks = models.JSONField(default=list, blank=True)
    success_factors = models.JSONField(default=list, blank=True)
    next_milestones = models.JSONField(default=list, blank=True)
    scoring_config_version_id = models.UUIDField(null=True, blank=True)
    status = models.CharField(max_length=12, choices=STATUS, default="draft")

    class Meta:
        db_table = "fundraising_strategy"


class StrategyAlternative(DealScopedModel):
    """≥3 alternatives (Doc 2 M2.6 §8.7)."""

    fundraising_strategy = models.ForeignKey(
        FundraisingStrategy, on_delete=models.CASCADE, related_name="alternatives")
    label = models.CharField(max_length=32)
    description = models.TextField(blank=True, default="")
    raise_value = models.DecimalField(max_digits=20, decimal_places=2,
                                      null=True, blank=True)
    raise_ccy = models.CharField(max_length=3, default="USD")
    raise_basis = models.CharField(max_length=32, default="recommendation")
    valuation_value = models.DecimalField(max_digits=20, decimal_places=2,
                                          null=True, blank=True)
    valuation_ccy = models.CharField(max_length=3, default="USD")
    valuation_basis = models.CharField(max_length=32, default="pre_money")
    dilution_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                       null=True, blank=True)
    runway_months = models.IntegerField(null=True, blank=True)
    investor_universe = models.JSONField(default=list, blank=True)
    advantages = models.JSONField(default=list, blank=True)
    disadvantages = models.JSONField(default=list, blank=True)
    execution_complexity = models.CharField(max_length=16, blank=True, default="")
    probability_of_success = models.CharField(max_length=16, blank=True, default="")
    is_selected = models.BooleanField(default=False)

    class Meta:
        db_table = "strategy_alternative"


class StrategyProfile(DealScopedModel):
    """M2.7 — the governing artefact (BR-M0-020): immutable once approved;
    material change → new version. Referenced by all Stage-3 tables."""

    STATUS = (("approved", "approved"), ("superseded", "superseded"))

    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    status = models.CharField(max_length=12, choices=STATUS, default="approved")
    approved_raise_value = models.DecimalField(max_digits=20, decimal_places=2,
                                               null=True, blank=True)
    approved_raise_ccy = models.CharField(max_length=3, default="USD")
    approved_raise_basis = models.CharField(max_length=32, default="approved")
    val_range_low_value = models.DecimalField(max_digits=20, decimal_places=2,
                                              null=True, blank=True)
    val_range_low_ccy = models.CharField(max_length=3, default="USD")
    val_range_low_basis = models.CharField(max_length=32, default="indicative")
    val_range_high_value = models.DecimalField(max_digits=20, decimal_places=2,
                                               null=True, blank=True)
    val_range_high_ccy = models.CharField(max_length=3, default="USD")
    val_range_high_basis = models.CharField(max_length=32, default="indicative")
    peer_universe = models.ForeignKey(PeerUniverse, null=True, blank=True,
                                      on_delete=models.SET_NULL, related_name="+")
    preferred_investor_categories = models.JSONField(default=dict, blank=True)
    fundraising_timeline = models.CharField(max_length=32, blank=True, default="")
    key_positioning = models.TextField(blank=True, default="")
    principal_investment_thesis = models.TextField(blank=True, default="")
    risks_mitigations = models.JSONField(default=list, blank=True)
    approved_assumptions = models.JSONField(default=list, blank=True)
    selected_alternative = models.ForeignKey(
        StrategyAlternative, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="+")
    selected_instrument = models.CharField(max_length=32, blank=True, default="")
    approval_timestamp = models.DateTimeField(null=True, blank=True)
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                    blank=True, on_delete=models.SET_NULL,
                                    related_name="+")

    class Meta:
        db_table = "strategy_profile"
        constraints = [
            models.UniqueConstraint(
                fields=["deal_id"],
                condition=models.Q(is_active=True) & models.Q(is_deleted=False),
                name="uq_active_strategy_live"),
        ]

    def as_context(self):
        """Shape consumed by the AI Context Engine."""
        return {
            "approvedRaise": {"value": float(self.approved_raise_value or 0),
                              "ccy": self.approved_raise_ccy},
            "valRangeLow": {"value": float(self.val_range_low_value or 0),
                            "ccy": self.val_range_low_ccy},
            "valRangeHigh": {"value": float(self.val_range_high_value or 0),
                             "ccy": self.val_range_high_ccy},
            "preferredInvestorCategories": self.preferred_investor_categories,
            "fundraisingTimeline": self.fundraising_timeline,
            "keyPositioning": self.key_positioning,
            "principalInvestmentThesis": self.principal_investment_thesis,
            "risksMitigations": self.risks_mitigations,
            "selectedInstrument": self.selected_instrument,
            "version": self.version_no,
        }
