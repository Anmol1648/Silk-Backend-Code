"""Deal Assessment — Fundraising Journey Spec, Part 6.1.

TWO KINDS OF TABLE LIVE HERE, and the distinction matters:

  CONFIG (ConfigParameter, ConfigWeight, ConfigRubric, ConfigAnchor)
      The scoring MODEL. Platform-level and versioned — not tenant data.
      One correct set of rubrics serves every tenant, because the model is
      the product. A tenant may override a row, but almost none should.

  ASSESSMENT DATA (Assessment, ParameterValue, CategoryScore, ...)
      One company's answers and results. Tenant-scoped, never overwritten —
      a re-run creates a new version and marks the old one superseded, so a
      score shown to an investor last month can still be reproduced exactly.

DOMAIN AGNOSTIC BY CONSTRUCTION. Nothing here names a company, a sector or a
currency. Sectors and sub-sectors are DATA (SectorDealData rows), stages are
config, and every threshold is a config row. The engine works for a tyre
marketplace, a healthcare app or a chemicals manufacturer without a code
change — the sample workbooks that informed the spec are examples, not the
model.
"""
import uuid

from django.conf import settings
from django.db import models

# §2.5 — stage selects which column-set of cut-points applies. Stored as
# plain strings so a tenant can add a stage ("Pre-Seed", "Series C") by
# adding rubric rows, without a migration.
DEAL_STAGES = [
    ("Seed", "Seed"),
    ("Series A", "Series A"),
    ("Series B", "Series B"),
    ("Growth", "Growth"),
]

BANDS = [
    ("Exceptional", "Exceptional (10) — override only"),
    ("Excellent", "Excellent (9)"),
    ("Good", "Good (7)"),
    ("Fair", "Fair (5)"),
    ("Poor", "Poor (3)"),
]

SCORING_TYPES = [
    ("numeric", "Numeric — judged against stage rubric cut-points"),
    ("range", "Range — ideal band with tolerance zones"),
    ("anchor", "Anchor — evidence matched to written band definitions"),
    ("computed", "Computed — rolled up from children, never entered"),
    ("lookup", "DB lookup — from the sector percentile model"),
]


class ConfigVersionedMixin(models.Model):
    """Config rows are versioned, never edited in place.

    The spec is explicit: changing a threshold silently re-rates every
    assessment built on it. Versioning means an assessment can record which
    config produced it, so a historical score stays reproducible after the
    product owner tightens a cut-point.
    """

    version = models.IntegerField(default=1, db_index=True)
    effective_from = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)
    tenant_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="NULL = platform default, used by every tenant. Set only "
                  "to override the model for one tenant — rare, and it "
                  "forks them off the platform scoring model.")
    notes = models.TextField(blank=True, default="")

    class Meta:
        abstract = True


class ConfigParameter(ConfigVersionedMixin):
    """One row per assessable parameter (§2.2 — the 80-parameter dictionary).

    `feeds_score=False` are the spec's `ref` rows: collected and displayed as
    cross-checks, excluded from the roll-up. A contradiction between a scored
    parameter and its ref sibling is a finding, not something to average away.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    input_key = models.CharField(max_length=48, db_index=True)
    ref_code = models.CharField(max_length=16, blank=True, default="",
                                help_text="Workbook ID, e.g. A.1.a")
    category_code = models.CharField(max_length=4, db_index=True)
    parent_code = models.CharField(
        max_length=16, blank=True, default="",
        help_text="Ref code of the parent roll-up node, blank for a "
                  "category-level child.")
    name = models.CharField(max_length=255)
    unit = models.CharField(max_length=32, blank=True, default="")
    scoring_type = models.CharField(max_length=16, choices=SCORING_TYPES,
                                    default="numeric")
    feeds_score = models.BooleanField(default=True)
    sort_order = models.IntegerField(default=0)

    # ---- Ingested from the workbook (v24) ------------------------------
    # Everything below is READ FROM THE SHEET, never hand-written. The four
    # naming and scoring defects found in v23 all had the same cause: config
    # derived twice, once by whoever transcribed the spec and once by the
    # seeder, with both copies free to drift from the workbook that is
    # actually authoritative.
    is_workbook_input = models.BooleanField(
        default=True,
        help_text="True for the 80 rows of the workbook's Company Profile "
                  "input sheet. False for nodes the workbook computes rather "
                  "than collects — the deal-database percentiles — which must "
                  "be excluded from the 'inputs captured (of 80)' count.")
    benchmark_level = models.CharField(
        max_length=12, blank=True, default="",
        help_text="sector | sub_sector — for percentile nodes, which "
                  "population this reads from.")
    benchmark_metric = models.CharField(
        max_length=16, blank=True, default="",
        help_text="velocity | ticket | investors — which stored percentile "
                  "column feeds this node.")
    companion_key = models.CharField(
        max_length=48, blank=True, default="",
        help_text="A second input this node is scored jointly with. E.1/F.1 "
                  "take the WORSE of TAM size and TAM growth, because a large "
                  "but stagnant market is not an attractive one.")

    # Extraction guidance, lifted verbatim from the Data Dictionary sheet.
    # This is what the sheet tells a human analyst to do, and it makes a far
    # better extraction instruction than anything written from scratch:
    # "treat an unmentioned seat as vacant, not as unknown".
    scoring_method = models.CharField(max_length=32, blank=True, default="")
    definition = models.TextField(blank=True, default="")
    where_to_find = models.TextField(blank=True, default="")
    if_missing = models.TextField(blank=True, default="")

    class Meta:
        db_table = "assess_config_parameter"
        indexes = [models.Index(fields=["tenant_id", "input_key", "-version"])]

    def __str__(self):
        return f"{self.ref_code or self.input_key} · {self.name}"


class ConfigWeight(ConfigVersionedMixin):
    """Category / sub-item / child weights (§2.1, §2.3).

    Integrity check #1: the seven category weights must sum to 100.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    level = models.CharField(
        max_length=16,
        choices=[("category", "Category"), ("subitem", "Sub-item"),
                 ("child", "Child")])
    code = models.CharField(max_length=16, db_index=True)
    parent_code = models.CharField(max_length=16, blank=True, default="")
    label = models.CharField(max_length=255, blank=True, default="")
    weight = models.DecimalField(max_digits=7, decimal_places=4)

    class Meta:
        db_table = "assess_config_weight"
        indexes = [models.Index(fields=["tenant_id", "level", "-version"])]

    def __str__(self):
        return f"{self.code} @ {self.weight}%"


class ConfigRubric(ConfigVersionedMixin):
    """Stage-dependent numeric cut-points (§2.5).

    One row per (input_key, stage). A 14-month runway is Fair at Series A and
    Good at Growth — which is exactly why stage must be set before scoring,
    and why the engine refuses to guess it silently.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    input_key = models.CharField(max_length=48, db_index=True)
    ref_code = models.CharField(max_length=16, blank=True, default="")
    metric_name = models.CharField(max_length=255, blank=True, default="")
    unit = models.CharField(max_length=32, blank=True, default="")
    direction = models.CharField(
        max_length=8,
        choices=[("Higher", "Higher is better"), ("Lower", "Lower is better"),
                 ("Range", "Ideal range")])
    stage = models.CharField(max_length=24, choices=DEAL_STAGES,
                             db_index=True)

    # Monotonic cut-points (inclusive).
    cut_excellent = models.DecimalField(max_digits=16, decimal_places=4,
                                        null=True, blank=True)
    cut_good = models.DecimalField(max_digits=16, decimal_places=4,
                                   null=True, blank=True)
    cut_fair = models.DecimalField(max_digits=16, decimal_places=4,
                                   null=True, blank=True)

    # Range type.
    ideal_min = models.DecimalField(max_digits=16, decimal_places=4,
                                    null=True, blank=True)
    ideal_max = models.DecimalField(max_digits=16, decimal_places=4,
                                    null=True, blank=True)
    good_tolerance_pct = models.DecimalField(max_digits=7, decimal_places=2,
                                             null=True, blank=True)
    fair_tolerance_pct = models.DecimalField(max_digits=7, decimal_places=2,
                                             null=True, blank=True)

    rationale = models.TextField(
        blank=True, default="",
        help_text="Why this cut-point. Shown to the founder so a score is "
                  "explainable rather than asserted.")

    class Meta:
        db_table = "assess_config_rubric"
        indexes = [
            models.Index(fields=["tenant_id", "input_key", "stage",
                                 "-version"]),
        ]

    def __str__(self):
        return f"{self.input_key} @ {self.stage}"


class ConfigAnchor(ConfigVersionedMixin):
    """Qualitative band definitions (§2.6) — stage-invariant.

    The LLM is given these four definitions and asked which the evidence
    actually satisfies, with the spec's rule carried verbatim: if the
    evidence does not clearly meet a band, score the band BELOW it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    input_key = models.CharField(max_length=48, db_index=True)
    ref_code = models.CharField(max_length=16, blank=True, default="")
    parameter_name = models.CharField(max_length=255, blank=True, default="")
    scoring_basis = models.CharField(max_length=64, blank=True, default="")
    excellent_def = models.TextField(blank=True, default="")
    good_def = models.TextField(blank=True, default="")
    fair_def = models.TextField(blank=True, default="")
    poor_def = models.TextField(blank=True, default="")
    evidence_required = models.TextField(blank=True, default="")

    class Meta:
        db_table = "assess_config_anchor"
        indexes = [models.Index(fields=["tenant_id", "input_key", "-version"])]

    def __str__(self):
        return f"{self.input_key} · {self.parameter_name}"


class ConfigConstant(ConfigVersionedMixin):
    """Scalar constants the workbook holds on its Deal Scorecard panel.

    These were hardcoded in Python and two of them were wrong. They belong
    here because the sheet calls them "editable" and the product owner is
    expected to move them — the percentile band thresholds and the minimum
    deal count for a reliable percentile are tuning dials, not physics.

    Known keys:
      score_excellent / score_good / score_fair / score_poor   9 / 7 / 5 / 3
      percentile_excellent / _good / _fair                     8 / 6 / 4
      min_deals_for_percentile                                 5
      rating_<band>                                            ladder cut-offs
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=64, db_index=True)
    value = models.DecimalField(max_digits=16, decimal_places=4)
    label = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "assess_config_constant"
        indexes = [models.Index(fields=["tenant_id", "key", "-version"])]

    def __str__(self):
        return f"{self.key}={self.value}"


class SectorDealData(models.Model):
    """Sector / sub-sector percentile inputs (§2.7). Pure DATA, refreshed
    periodically — adding a sector is a data load, never a code change."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    level = models.CharField(max_length=12,
                             choices=[("sector", "Sector"),
                                      ("sub_sector", "Sub-sector")])
    name = models.CharField(max_length=255, db_index=True)
    deal_count = models.IntegerField(null=True, blank=True)
    total_raised_usd_mn = models.DecimalField(max_digits=16, decimal_places=2,
                                              null=True, blank=True)
    avg_ticket_usd_mn = models.DecimalField(max_digits=16, decimal_places=2,
                                            null=True, blank=True)
    investor_count = models.IntegerField(null=True, blank=True)
    as_of_date = models.DateField(null=True, blank=True)

    # Percentile scores, 0-10, derived on import by ranking every row at the
    # same level against its peers. Stored rather than computed at read time
    # because the rank depends on the whole population: recomputing later,
    # after a partial refresh, would silently change the score of a row
    # nobody touched.
    velocity_score = models.DecimalField(max_digits=4, decimal_places=2,
                                         null=True, blank=True)
    ticket_score = models.DecimalField(max_digits=4, decimal_places=2,
                                       null=True, blank=True)
    investors_score = models.DecimalField(max_digits=4, decimal_places=2,
                                          null=True, blank=True)
    clubbed_group = models.CharField(
        max_length=255, blank=True, default="", db_index=True,
        help_text="For sub-sector rows: the group this raw label rolls up "
                  "into. Scoring uses the group, not the raw label.")
    sample_quality = models.CharField(
        max_length=32, blank=True, default="",
        help_text="OK, or Thin when the deal count is too small for the "
                  "percentile to mean anything.")
    source_file = models.CharField(max_length=255, blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "assess_sector_deal_data"
        unique_together = [("level", "name")]

    def __str__(self):
        return f"{self.level}:{self.name}"


class SectorMapping(models.Model):
    """Raw sector label -> clubbed group (§2.7).

    Free-text sector labels arrive from a dozen sources spelled a dozen ways.
    Mapping them is data, so a new label is a row, not a deploy.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_label = models.CharField(max_length=255, unique=True)
    clubbed_group = models.CharField(max_length=255, db_index=True)

    class Meta:
        db_table = "assess_sector_mapping"


class Assessment(models.Model):
    """One scoring run for one company. NEVER overwritten (Part 8 #2).

    A re-run creates a new row and points the old one at it via
    superseded_by, so a score quoted to an investor stays reproducible.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    company = models.ForeignKey("core.Company", on_delete=models.CASCADE,
                                related_name="assessments")
    deal = models.ForeignKey("core.Deal", null=True, blank=True,
                             on_delete=models.SET_NULL, related_name="+")
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    deal_stage = models.CharField(max_length=24, choices=DEAL_STAGES,
                                  blank=True, default="")
    stage_was_defaulted = models.BooleanField(
        default=False,
        help_text="True when stage was unset and defaulted to Series A. The "
                  "spec requires this be flagged loudly, never silent — the "
                  "wrong stage re-bands every numeric parameter.")
    sector = models.CharField(max_length=255, blank=True, default="")
    sub_sector = models.CharField(max_length=255, blank=True, default="")
    assessment_date = models.DateField(null=True, blank=True)

    fx_rate = models.DecimalField(max_digits=12, decimal_places=4, null=True,
                                  blank=True)
    capital_raised_usd_mn = models.DecimalField(max_digits=16,
                                                decimal_places=2, null=True,
                                                blank=True)
    premoney_ask_inr_cr = models.DecimalField(max_digits=16, decimal_places=2,
                                              null=True, blank=True)

    overall_score = models.DecimalField(max_digits=5, decimal_places=2,
                                        null=True, blank=True)
    rating_band = models.CharField(max_length=32, blank=True, default="")
    input_coverage_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                             default=0)
    audit_status = models.CharField(
        max_length=16, default="pending",
        choices=[("pending", "Pending"), ("passed", "Passed"),
                 ("failed", "Failed integrity checks")])
    audit_findings = models.JSONField(default=list, blank=True)
    config_version = models.IntegerField(
        default=1,
        help_text="Config generation that produced this score. Without it a "
                  "historical assessment cannot be reproduced after the "
                  "product owner changes a threshold.")

    status = models.CharField(
        max_length=16, default="draft",
        choices=[("draft", "Draft"), ("scored", "Scored"),
                 ("final", "Finalized"), ("superseded", "Superseded")])
    superseded_by = models.ForeignKey("self", null=True, blank=True,
                                      on_delete=models.SET_NULL,
                                      related_name="supersedes")

    class Meta:
        db_table = "assess_assessment"
        indexes = [models.Index(fields=["company", "-created_at"]),
                   models.Index(fields=["tenant_id", "-created_at"])]

    def __str__(self):
        return f"{self.company_id} · {self.deal_stage} · {self.rating_band}"


class ParameterValue(models.Model):
    """One row per parameter per assessment (§6.1, §4.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE,
                                   related_name="parameter_values")
    input_key = models.CharField(max_length=48, db_index=True)
    ref_code = models.CharField(max_length=16, blank=True, default="")
    category = models.CharField(max_length=4, blank=True, default="")

    raw_value = models.CharField(max_length=255, blank=True, default="")
    unit = models.CharField(max_length=32, blank=True, default="")
    band = models.CharField(max_length=16, choices=BANDS, blank=True,
                            default="")
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True,
                                blank=True)
    is_reference_only = models.BooleanField(default=False)

    # Provenance (§4.3) — a score with no traceable source is an opinion.
    source_type = models.CharField(max_length=32, blank=True, default="")
    source_detail = models.TextField(blank=True, default="")
    source_tier = models.IntegerField(null=True, blank=True)
    confidence = models.DecimalField(max_digits=4, decimal_places=2,
                                     null=True, blank=True)
    justification = models.TextField(
        blank=True, default="",
        help_text="For anchor-scored parameters, why the evidence meets this "
                  "band rather than the one above.")

    # Override (§2.4.4) — the only route to Exceptional/10.
    system_band = models.CharField(max_length=16, blank=True, default="")
    system_score = models.DecimalField(max_digits=5, decimal_places=2,
                                       null=True, blank=True)
    is_overridden = models.BooleanField(default=False)
    override_comment = models.TextField(blank=True, default="")
    overridden_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                      blank=True, on_delete=models.SET_NULL,
                                      related_name="+")
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "assess_parameter_value"
        unique_together = [("assessment", "input_key")]

    def __str__(self):
        return f"{self.input_key}={self.raw_value} [{self.band}]"


class CategoryScore(models.Model):
    """Seven rows per assessment (§6.1).

    applied_weight is the share the category actually carried once unscored
    siblings were excluded — surfaced so a founder can see which answers are
    carrying more weight than their nominal share.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE,
                                   related_name="category_scores")
    category_code = models.CharField(max_length=4)
    label = models.CharField(max_length=64, blank=True, default="")
    weight = models.DecimalField(max_digits=7, decimal_places=4, default=0)
    applied_weight = models.DecimalField(max_digits=7, decimal_places=4,
                                         default=0)
    score = models.DecimalField(max_digits=5, decimal_places=2, null=True,
                                blank=True)
    breakdown = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "assess_category_score"
        unique_together = [("assessment", "category_code")]


class ValuationPeerComp(models.Model):
    """Peer comparables feeding Phase 2 (§2.8)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE,
                                   related_name="peer_comps")
    comp_name = models.CharField(max_length=255)
    date = models.DateField(null=True, blank=True)
    country = models.CharField(max_length=64, blank=True, default="")
    revenue_inr_cr = models.DecimalField(max_digits=16, decimal_places=2,
                                         null=True, blank=True)
    ev_revenue_x = models.DecimalField(max_digits=10, decimal_places=2,
                                       null=True, blank=True)
    ev_ebitda_x = models.DecimalField(max_digits=10, decimal_places=2,
                                      null=True, blank=True)
    source = models.CharField(max_length=512, blank=True, default="")
    why_comparable = models.TextField(blank=True, default="")

    class Meta:
        db_table = "assess_valuation_peer_comp"


class ValuationOutput(models.Model):
    """Phase 2 result plus the founder's accepted figures (§2.8).

    The founder's number is stored ALONGSIDE the computed one, never over
    it — the gap between what the model implies and what the founder will
    accept is itself signal for Phase 3.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.OneToOneField(Assessment, on_delete=models.CASCADE,
                                      related_name="valuation")
    peer_median_ev_revenue = models.DecimalField(max_digits=10,
                                                 decimal_places=2, null=True,
                                                 blank=True)
    peer_median_ev_ebitda = models.DecimalField(max_digits=10,
                                                decimal_places=2, null=True,
                                                blank=True)
    implied_premoney_inr_cr = models.DecimalField(max_digits=16,
                                                  decimal_places=2, null=True,
                                                  blank=True)
    premium_discount_pct = models.DecimalField(max_digits=8, decimal_places=2,
                                               null=True, blank=True)
    suggested_raise_usd_mn = models.DecimalField(max_digits=16,
                                                 decimal_places=2, null=True,
                                                 blank=True)
    founder_accepted_valuation_inr_cr = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True)
    founder_accepted_raise_usd_mn = models.DecimalField(
        max_digits=16, decimal_places=2, null=True, blank=True)
    founder_edit_reason = models.TextField(blank=True, default="")

    class Meta:
        db_table = "assess_valuation_output"


class InvestorProfile(models.Model):
    """Investor universe for matching (Part 7).

    Deliberately generic: stage/sector/geography focus are free-text arrays
    and cheque size is a numeric range, so the same table serves any domain.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    name_or_avatar_label = models.CharField(max_length=255)
    is_named_entity = models.BooleanField(
        default=True,
        help_text="False for anonymised 'investor archetype' rows, used when "
                  "the underlying source cannot be attributed.")
    type = models.CharField(max_length=64, blank=True, default="")
    stage_focus = models.JSONField(default=list, blank=True)
    sector_focus = models.JSONField(default=list, blank=True)
    geography_focus = models.JSONField(default=list, blank=True)
    cheque_size_min_usd = models.DecimalField(max_digits=16, decimal_places=2,
                                              null=True, blank=True)
    cheque_size_max_usd = models.DecimalField(max_digits=16, decimal_places=2,
                                              null=True, blank=True)
    source = models.CharField(max_length=512, blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "assess_investor_profile"


class InvestorMatch(models.Model):
    """Phase 3 output (Part 7.2). match_reasoning is mandatory in spirit —
    an unexplained match is not actionable for a founder."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE,
                                   related_name="investor_matches")
    investor = models.ForeignKey(InvestorProfile, on_delete=models.CASCADE,
                                 related_name="+")
    match_score = models.DecimalField(max_digits=6, decimal_places=2,
                                      default=0)
    match_reasoning = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "assess_investor_match"
        unique_together = [("assessment", "investor")]
        ordering = ["-match_score"]


class ReviewLog(models.Model):
    """Founder pushback trail (§6.1, Assessment Control §D).

    Kept because a founder disagreeing with a score is information about the
    model, not just about the deal — and a rejected challenge needs a record
    as much as an accepted one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True)
    assessment = models.ForeignKey(Assessment, on_delete=models.CASCADE,
                                   related_name="review_log")
    field_or_topic = models.CharField(max_length=128)
    previous_value = models.TextField(blank=True, default="")
    founder_value = models.TextField(blank=True, default="")
    founder_reason = models.TextField(blank=True, default="")
    agent_action = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=12, default="open",
        choices=[("open", "Open"), ("closed", "Closed"),
                 ("rejected", "Rejected")])
    raised_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                  blank=True, on_delete=models.SET_NULL,
                                  related_name="+")

    class Meta:
        db_table = "assess_review_log"
        ordering = ["-created_at"]
