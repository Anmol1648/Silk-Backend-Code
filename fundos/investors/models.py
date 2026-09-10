"""Investor universe, deal data and the matching engine's configuration.

WHY THIS APP EXISTS SEPARATELY FROM `assessment`
------------------------------------------------
`assessment.InvestorProfile` modelled an investor as a firm with a JSON blob
of focus areas. The source data is one row per investor PER DEAL — 7,777 rows
across 3,454 investors — and every step of the matching pipeline operates on
those deal rows: tier flags, band eligibility, sector match, near-distance.
Investor-level figures (average ticket, deal counts) are AGGREGATES over them.

Modelling this firm-first would make the tiering logic impossible to express
as a query and impossible to keep consistent across refreshes. So the base
table is InvestorDeal and Investor is the derived dimension.

FIVE FIELD GROUPS, AND THE DISTINCTION MATTERS ON EVERY REFRESH
---------------------------------------------------------------
  1. Deal facts        — from the source scrape. Refreshed weekly.
  2. Explode & counts  — computed from (1). Recomputed every refresh.
  3. Investor master   — category, HQ, bucket. LLM-classified for NEW names
                         only; existing rows are never re-classified.
  4. Relationship/CRM  — who we know, who we met, contact details. Entered by
                         humans, NEVER written by the pipeline. This is the
                         one group a scraper cannot rebuild, so the merge step
                         must preserve it. There is a test asserting this.
  5. Match helpers     — tier, scores, distances. NOT STORED. They are the
                         SQL of the match query, evaluated per request against
                         the current filters, because the filters are
                         user-adjustable by design.

DOMAIN AGNOSTIC BY CONSTRUCTION
-------------------------------
Nothing here names a sector, a country, a currency or a raise size. Sectors
are rows. Categories are rows. Raise bands are rows. Ranking weights are rows.
Onboarding a US SaaS company raising $40M is a data load, never a deploy.
"""
import uuid

from django.conf import settings
from django.db import models


# ---------------------------------------------------------------------------
# Sector taxonomy — replaces sector_normalizer.CANONICAL_ALIASES
# ---------------------------------------------------------------------------

class CanonicalSector(models.Model):
    """One row per canonical sector or sub-sector name.

    The supplied normalizer held 30 canonical sectors as a Python dict, with a
    sector_config.json merged over it at import time. That merge already proved
    the pattern — user-defined entries extend built-ins without editing code.
    This is that pattern, moved into the database where an admin can reach it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=255, db_index=True)
    level = models.CharField(
        max_length=12, default="sector",
        choices=[("sector", "Sector"), ("sub_sector", "Sub-sector")])
    parent = models.ForeignKey("self", null=True, blank=True,
                               on_delete=models.SET_NULL,
                               related_name="children",
                               help_text="Parent sector for a sub-sector. "
                                         "Drives the Tier 2 (sector) match.")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "inv_canonical_sector"
        ordering = ["level", "name"]
        # A label can legitimately exist at BOTH levels — "D2C" is a sector
        # and also a sub-sector in the source data — so uniqueness is per
        # level, not global.
        unique_together = [("name", "level")]

    def __str__(self):
        return self.name


class SectorAlias(models.Model):
    """A known spelling variant. Lowercased on save for O(1) exact lookup."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    alias = models.CharField(max_length=255, unique=True)
    sector = models.ForeignKey(CanonicalSector, on_delete=models.CASCADE,
                               related_name="aliases")
    created_by_admin = models.BooleanField(default=False)

    class Meta:
        db_table = "inv_sector_alias"

    def save(self, *args, **kwargs):
        self.alias = (self.alias or "").strip().lower()
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.alias} → {self.sector_id}"


class SectorGroup(models.Model):
    """Named grouping of sectors — replaces the hardcoded frozensets.

    The supplied code had IMPACTECH_SECTORS, FIG_FINTECH_SECTORS and
    DEEPTECH_AI_SECTORS as module-level constants, plus HIGHLIGHT_SECTORS in
    config. All four are the same idea: a named set of sectors used for
    grouping or emphasis. One table serves all of them and any future one.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128, unique=True)
    sectors = models.ManyToManyField(CanonicalSector, blank=True,
                                     related_name="groups")
    display_colour = models.CharField(max_length=7, blank=True, default="")

    class Meta:
        db_table = "inv_sector_group"

    def __str__(self):
        return self.name


class SectorReviewItem(models.Model):
    """A sector string that failed normalisation.

    The supplied normalizer appended these to sector_unknowns.log — correct
    instinct, wrong destination. A log file is written and never read. As a
    table it becomes a queue an admin can clear, with one click creating the
    alias that prevents the next occurrence.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_value = models.CharField(max_length=255, db_index=True)
    best_candidate = models.CharField(max_length=255, blank=True, default="")
    best_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    occurrences = models.IntegerField(default=1)
    first_seen = models.DateTimeField(auto_now_add=True)
    last_seen = models.DateTimeField(auto_now=True)
    status = models.CharField(
        max_length=12, default="open",
        choices=[("open", "Open"), ("resolved", "Resolved"),
                 ("ignored", "Ignored")])

    class Meta:
        db_table = "inv_sector_review"
        ordering = ["-occurrences", "-last_seen"]
        unique_together = [("raw_value",)]

    def __str__(self):
        return f"{self.raw_value} ({self.occurrences})"


# ---------------------------------------------------------------------------
# Investor taxonomy — the 14 source categories collapsing into 9 buckets
# ---------------------------------------------------------------------------

class InvestorBucket(models.Model):
    """Output grouping shown to the user (9 in the source workbook)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, unique=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "inv_bucket"
        ordering = ["sort_order", "name"]

    def __str__(self):
        return self.name


class InvestorCategory(models.Model):
    """Source-level classification (14 in the workbook), mapped to a bucket.

    Kept separate from the bucket because the classification an LLM makes is at
    this level ("Domestic Corp VC"), while the presentation is at bucket level
    ("Corp VC"). Collapsing them would lose the distinction the classifier is
    actually making, and the mapping is a business decision that changes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, unique=True)
    bucket = models.ForeignKey(InvestorBucket, on_delete=models.PROTECT,
                               related_name="categories")
    description = models.TextField(
        blank=True, default="",
        help_text="Shown to the classifier so it knows what this category "
                  "means. Keep it one plain sentence.")

    class Meta:
        db_table = "inv_category"
        ordering = ["name"]
        verbose_name_plural = "Investor categories"

    def __str__(self):
        return self.name


# ---------------------------------------------------------------------------
# Investor master
# ---------------------------------------------------------------------------

class Investor(models.Model):
    """One row per investing entity. The derived dimension over InvestorDeal.

    RELATIONSHIP FIELDS (the `rel_*` block) ARE NEVER WRITTEN BY THE PIPELINE.
    They are proprietary relationship capital entered by the deal team. A
    refresh that overwrites rel_relationship is a bug, not a merge conflict —
    see pipeline.merge_investor() and the test that asserts it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)

    # --- Identity -----------------------------------------------------------
    name = models.CharField(max_length=255, db_index=True)
    normalised_name = models.CharField(
        max_length=255, db_index=True,
        help_text="Lowercased, punctuation-stripped, legal suffixes removed. "
                  "The key identity resolution matches on.")
    aliases = models.JSONField(
        default=list, blank=True,
        help_text="Absorbed spellings, e.g. ['Accel Partners', 'Accel India'].")

    # --- Master attributes (group 3 — LLM-classified for new names only) -----
    category = models.ForeignKey(InvestorCategory, null=True, blank=True,
                                 on_delete=models.SET_NULL,
                                 related_name="investors")
    hq_city = models.CharField(max_length=128, blank=True, default="")
    hq_country = models.CharField(max_length=128, blank=True, default="")
    has_local_office = models.BooleanField(null=True, blank=True)
    single_sector_focused = models.BooleanField(null=True, blank=True)
    is_impact = models.BooleanField(null=True, blank=True)

    classification_source = models.CharField(
        max_length=24, blank=True, default="",
        choices=[("import", "Bulk import"), ("llm", "LLM classification"),
                 ("manual", "Set by an admin")])
    classification_confidence = models.DecimalField(
        max_digits=4, decimal_places=2, null=True, blank=True)
    classification_model = models.CharField(max_length=64, blank=True,
                                            default="")
    classified_at = models.DateTimeField(null=True, blank=True)

    # --- Relationship / CRM (group 4 — NEVER pipeline-written) --------------
    rel_internal_name = models.CharField(max_length=255, blank=True, default="")
    rel_owner = models.CharField(
        max_length=255, blank=True, default="",
        help_text="The person on our side who owns this relationship.")
    rel_met = models.BooleanField(null=True, blank=True)
    rel_relationship = models.CharField(max_length=64, blank=True, default="")
    rel_contact_name = models.CharField(max_length=255, blank=True, default="")
    rel_contact_title = models.CharField(max_length=128, blank=True, default="")
    rel_mobile = models.CharField(max_length=64, blank=True, default="")
    rel_emails = models.JSONField(default=list, blank=True)
    rel_past_outreach = models.BooleanField(null=True, blank=True)
    rel_past_revert = models.BooleanField(null=True, blank=True)
    rel_notes = models.TextField(blank=True, default="")

    # --- Derived aggregates (group 2 — recomputed every refresh) ------------
    overall_deals = models.IntegerField(default=0)
    avg_ticket_usd_mn = models.DecimalField(
        max_digits=16, decimal_places=4, null=True, blank=True,
        help_text="New ATS — the mean of fundraise_per_investor across this "
                  "investor's deals with a positive deal size.")
    last_deal_size_usd_mn = models.DecimalField(max_digits=16, decimal_places=4,
                                                null=True, blank=True)
    last_deal_date = models.DateField(null=True, blank=True)

    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "inv_investor"
        indexes = [
            models.Index(fields=["tenant_id", "normalised_name"]),
            models.Index(fields=["category"]),
        ]

    def __str__(self):
        return self.name

    # Field names the serializer must strip for a founder audience. Declared
    # here rather than in the view so a new relationship field cannot be added
    # without a decision about who sees it.
    RELATIONSHIP_FIELDS = (
        "rel_internal_name", "rel_owner", "rel_met", "rel_relationship",
        "rel_contact_name", "rel_contact_title", "rel_mobile", "rel_emails",
        "rel_past_outreach", "rel_past_revert", "rel_notes",
    )


class InvestorAliasCandidate(models.Model):
    """A name that fuzzy-matched an existing investor below the auto threshold.

    Business rule: anything below 90% goes to an LLM with web search for
    adjudication, and the LLM run is triggered MANUALLY by a config admin —
    never automatically as part of the refresh. A refresh that quietly merges
    'Accel India' into 'Accel' on a 0.87 score changes deal counts, which
    changes rankings, with nothing on screen looking wrong.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raw_name = models.CharField(max_length=255, db_index=True)
    candidate = models.ForeignKey(Investor, null=True, blank=True,
                                  on_delete=models.CASCADE, related_name="+")
    fuzzy_score = models.DecimalField(max_digits=5, decimal_places=2, default=0)
    alternatives = models.JSONField(
        default=list, blank=True,
        help_text="[{investorId, name, score}] — the runners-up, so an admin "
                  "sees what else it could have been.")
    occurrences = models.IntegerField(default=1)

    # --- LLM adjudication (manually triggered) ------------------------------
    llm_verdict = models.CharField(
        max_length=16, blank=True, default="",
        choices=[("same", "Same entity"), ("different", "Different entity"),
                 ("unclear", "Could not determine")])
    llm_confidence = models.DecimalField(max_digits=4, decimal_places=2,
                                         null=True, blank=True)
    llm_reasoning = models.TextField(blank=True, default="")
    llm_sources = models.JSONField(default=list, blank=True)
    llm_run_at = models.DateTimeField(null=True, blank=True)
    llm_run_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    status = models.CharField(
        max_length=16, default="pending", db_index=True,
        choices=[("pending", "Awaiting review"),
                 ("llm_done", "LLM adjudicated — awaiting admin"),
                 ("merged", "Merged"), ("separate", "Kept separate")])
    resolved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                    blank=True, on_delete=models.SET_NULL,
                                    related_name="+")
    resolved_at = models.DateTimeField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inv_alias_candidate"
        ordering = ["-occurrences", "-created_at"]

    def __str__(self):
        return f"{self.raw_name} ~ {self.candidate_id} @ {self.fuzzy_score}"


class ClassificationReviewItem(models.Model):
    """An LLM classification that landed below the confidence threshold."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE,
                                 related_name="classification_reviews")
    proposed_category = models.ForeignKey(InvestorCategory, null=True,
                                          blank=True,
                                          on_delete=models.SET_NULL,
                                          related_name="+")
    proposed_hq_city = models.CharField(max_length=128, blank=True, default="")
    proposed_hq_country = models.CharField(max_length=128, blank=True,
                                           default="")
    confidence = models.DecimalField(max_digits=4, decimal_places=2, default=0)
    reasoning = models.TextField(blank=True, default="")
    status = models.CharField(
        max_length=12, default="open",
        choices=[("open", "Open"), ("accepted", "Accepted"),
                 ("corrected", "Corrected"), ("rejected", "Rejected")])
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inv_classification_review"
        ordering = ["-created_at"]


# ---------------------------------------------------------------------------
# Deal data
# ---------------------------------------------------------------------------

class FundingDeal(models.Model):
    """One funding event, as scraped. Sector strings stored VERBATIM.

    The supplied funding_db.py stored raw sector strings and normalised only
    in memory — the right call, and kept here. Normalisation is a view of the
    data, not the data. Re-tuning the alias table then re-deriving is cheap;
    recovering an original string that was overwritten is impossible.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source = models.CharField(max_length=64, default="inc42", db_index=True)
    source_url = models.URLField(max_length=1024, blank=True, default="")

    company_name = models.CharField(max_length=255, db_index=True)
    deal_date = models.DateField(null=True, blank=True, db_index=True)
    iso_week = models.IntegerField(null=True, blank=True)
    iso_year = models.IntegerField(null=True, blank=True)

    raw_sector = models.CharField(max_length=255, blank=True, default="")
    raw_sub_sector = models.CharField(max_length=255, blank=True, default="")
    sector = models.ForeignKey(CanonicalSector, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    sub_sector = models.ForeignKey(CanonicalSector, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="+")

    business_model = models.CharField(max_length=128, blank=True, default="")
    round_type = models.CharField(max_length=128, blank=True, default="")
    raw_amount = models.CharField(max_length=128, blank=True, default="")
    amount_usd_mn = models.DecimalField(max_digits=16, decimal_places=4,
                                        null=True, blank=True, db_index=True)

    raw_investors = models.TextField(blank=True, default="")
    raw_lead_investor = models.CharField(max_length=512, blank=True, default="")

    dedup_key = models.CharField(max_length=512, unique=True, db_index=True)
    first_seen_run = models.UUIDField(null=True, blank=True)
    last_seen_run = models.UUIDField(null=True, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inv_funding_deal"
        ordering = ["-deal_date", "-amount_usd_mn"]
        indexes = [models.Index(fields=["-deal_date", "-amount_usd_mn"])]

    def __str__(self):
        return f"{self.company_name} · {self.deal_date} · {self.amount_usd_mn}"


class InvestorDeal(models.Model):
    """One investor's participation in one deal. THE BASE TABLE.

    Every match query runs over these rows. Denormalised deliberately: sector,
    sub_sector, deal_date and deal_size are copied from FundingDeal so the
    matching query is a single-table scan with no joins on the hot path. The
    workbook does the same thing for the same reason.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)

    investor = models.ForeignKey(Investor, on_delete=models.CASCADE,
                                 related_name="deals")
    funding_deal = models.ForeignKey(FundingDeal, on_delete=models.CASCADE,
                                     related_name="investor_rows")

    # Denormalised from the parent deal — the matching hot path.
    deal_date = models.DateField(null=True, blank=True, db_index=True)
    deal_size_usd_mn = models.DecimalField(max_digits=16, decimal_places=4,
                                           null=True, blank=True,
                                           db_index=True)
    sector = models.ForeignKey(CanonicalSector, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    sub_sector = models.ForeignKey(CanonicalSector, null=True, blank=True,
                                   on_delete=models.SET_NULL,
                                   related_name="+")
    company_name = models.CharField(max_length=255, blank=True, default="")

    is_lead = models.BooleanField(default=False)
    investors_in_deal = models.IntegerField(default=1)
    fundraise_per_investor = models.DecimalField(
        max_digits=16, decimal_places=4, null=True, blank=True,
        help_text="deal_size / investors_in_deal. Averaging this across an "
                  "investor's deals gives their average ticket (New ATS).")

    class Meta:
        db_table = "inv_investor_deal"
        unique_together = [("investor", "funding_deal")]
        indexes = [
            models.Index(fields=["investor", "deal_date"]),
            models.Index(fields=["deal_date", "deal_size_usd_mn"]),
            models.Index(fields=["sub_sector", "deal_date"]),
        ]

    def __str__(self):
        return f"{self.investor_id} @ {self.company_name}"


# ---------------------------------------------------------------------------
# Matching configuration
# ---------------------------------------------------------------------------

class RaiseBand(models.Model):
    """Raise size → cheque band lookup.

    Resolution is exact-or-next-smaller (the workbook's XLOOKUP match_mode
    -1); below the smallest row, the smallest row applies. The supplied table
    has three rows topping out at a $5M raise, which will not carry Series B
    or Growth — but that is a data gap, not a code one. Add rows.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    raise_size_usd_mn = models.DecimalField(max_digits=16, decimal_places=4,
                                            unique=True)
    min_cheque_usd_mn = models.DecimalField(max_digits=16, decimal_places=4)
    max_cheque_usd_mn = models.DecimalField(max_digits=16, decimal_places=4)
    notes = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "inv_raise_band"
        ordering = ["raise_size_usd_mn"]

    def __str__(self):
        return (f"{self.raise_size_usd_mn} → "
                f"{self.min_cheque_usd_mn}–{self.max_cheque_usd_mn}")


class MatchConfig(models.Model):
    """Every tuneable in the matching engine. One active row.

    In the workbook these were cells: Dyn Engine!AH23/AH24 for the ranking
    weights, AH28 for the PE split, Control Panel C8/C9 for exclusivity. They
    were always configuration; here they are reachable.
    """

    BAND_BASIS = [
        ("deal_size", "Deal size — has this investor been in rounds this big?"),
        ("investor_ats", "Investor ATS — does this investor write cheques this big?"),
    ]

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, default="default")
    is_active = models.BooleanField(default=True)

    # Ranking (tiers 3 and 4).
    activity_weight = models.DecimalField(max_digits=4, decimal_places=3,
                                          default=0.700)
    ticket_fit_weight = models.DecimalField(max_digits=4, decimal_places=3,
                                            default=0.300)
    activity_constant = models.IntegerField(
        default=3,
        help_text="k in ScopeCount/(ScopeCount+k). Higher k means activity "
                  "saturates more slowly, favouring prolific investors.")

    # Band test basis — CONFIRMED as deal size, matching the workbook.
    # The written requirement said investor ATS; the workbook computes ATS and
    # never references it in the engine. Deal size is what produces the
    # published 301/477 reference counts.
    band_test_basis = models.CharField(max_length=16, choices=BAND_BASIS,
                                       default="deal_size")

    # Exclusivity — identifies sector specialists.
    exclusivity_threshold = models.DecimalField(max_digits=5, decimal_places=4,
                                                default=1.0)
    exclusivity_min_deals = models.IntegerField(default=1)
    pe_split_threshold = models.IntegerField(default=20)

    default_window_months = models.IntegerField(
        default=30, help_text="Default date window when the caller gives none.")

    updated_at = models.DateTimeField(auto_now=True)
    updated_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL,
                                   related_name="+")

    class Meta:
        db_table = "inv_match_config"

    def __str__(self):
        return f"{self.name} ({'active' if self.is_active else 'inactive'})"

    @classmethod
    def active(cls):
        return cls.objects.filter(is_active=True).first() or cls()


# ---------------------------------------------------------------------------
# Shortlist
# ---------------------------------------------------------------------------

class InvestorShortlist(models.Model):
    """Saved-for-outreach. Honestly a shortlist and nothing more (D-14).

    Deliberately not called a pipeline. Stage 4 does not exist yet, and a
    counter labelled "outreach" that tracks nothing is worse than a bookmark
    labelled as a bookmark.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    deal = models.ForeignKey("core.Deal", on_delete=models.CASCADE,
                             related_name="investor_shortlist")
    investor = models.ForeignKey(Investor, on_delete=models.CASCADE,
                                 related_name="+")
    match_score = models.DecimalField(max_digits=6, decimal_places=2,
                                      null=True, blank=True)
    tier_at_save = models.IntegerField(null=True, blank=True)
    rationale = models.TextField(blank=True, default="")
    note = models.TextField(blank=True, default="")
    saved_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                 blank=True, on_delete=models.SET_NULL,
                                 related_name="+")
    saved_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "inv_shortlist"
        unique_together = [("deal", "investor")]
        ordering = ["-saved_at"]


# ---------------------------------------------------------------------------
# Refresh: sources, schedule, run history
# ---------------------------------------------------------------------------

class DataSource(models.Model):
    """A place deal data comes from.

    The supplied code hardcoded Inc42's URL, the ten-week window, the polite
    delay and the circuit-breaker thresholds as module constants. All of them
    are per-source operational settings, so they belong on the source row —
    which is also what makes a second source an adapter plus a row rather than
    a code change.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, unique=True)
    adapter = models.CharField(
        max_length=64, default="inc42_funding_galore",
        help_text="Registered adapter key. See fundos/investors/sources.py.")
    base_url = models.URLField(max_length=1024, blank=True, default="")
    enabled = models.BooleanField(default=True)

    articles_per_run = models.IntegerField(
        default=10,
        help_text="Rolling window of source articles to read each run.")
    manual_urls = models.JSONField(
        default=list, blank=True,
        help_text="Read FIRST, before discovered URLs. For reports published "
                  "but not yet indexed on the source's own listing page.")
    request_delay_seconds = models.DecimalField(max_digits=5, decimal_places=2,
                                                default=0.5)
    max_retries = models.IntegerField(default=3)
    breaker_failure_threshold = models.IntegerField(default=3)
    breaker_recovery_seconds = models.IntegerField(default=300)
    browser_path = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Headless browser executable. Blank uses the bundled one.")

    last_run_at = models.DateTimeField(null=True, blank=True)
    last_run_status = models.CharField(max_length=16, blank=True, default="")

    class Meta:
        db_table = "inv_data_source"

    def __str__(self):
        return self.name


class RefreshSchedule(models.Model):
    """When the refresh runs. Default Saturday 18:00, editable.

    The supplied stack had no scheduler at all — main.py ran on invocation and
    the cadence lived in an OS task. Making the cron expression a field means
    changing the cadence is an admin edit with a visible next-run time, not a
    server login.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=64, default="weekly")
    enabled = models.BooleanField(default=True)
    day_of_week = models.IntegerField(
        default=5, help_text="0=Monday … 5=Saturday, 6=Sunday.")
    hour = models.IntegerField(default=18)
    minute = models.IntegerField(default=0)
    timezone_name = models.CharField(max_length=64, default="Asia/Kolkata")
    source = models.ForeignKey(DataSource, null=True, blank=True,
                               on_delete=models.SET_NULL, related_name="+")
    last_fired_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "inv_refresh_schedule"

    def __str__(self):
        days = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
        d = days[self.day_of_week] if 0 <= self.day_of_week < 7 else "?"
        return f"{d} {self.hour:02d}:{self.minute:02d} {self.timezone_name}"

    def next_run(self):
        """Resolved next fire time, so an edit is visibly confirmed."""
        import datetime as _dt
        try:
            from zoneinfo import ZoneInfo
            tz = ZoneInfo(self.timezone_name)
        except Exception:
            tz = _dt.timezone.utc
        now = _dt.datetime.now(tz)
        ahead = (self.day_of_week - now.weekday()) % 7
        candidate = now.replace(hour=self.hour, minute=self.minute,
                                second=0, microsecond=0) + _dt.timedelta(days=ahead)
        if candidate <= now:
            candidate += _dt.timedelta(days=7)
        return candidate


class RefreshRun(models.Model):
    """One execution of the pipeline — dry run or commit.

    Every run is a queryable row, not a log line. "It worked last week" has to
    be answerable, and a truncated app.log cannot answer it.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run_type = models.CharField(
        max_length=16, default="commit",
        choices=[("dry_run", "Dry run — nothing written"),
                 ("commit", "Commit"), ("scheduled", "Scheduled")])
    source_name = models.CharField(max_length=64, blank=True, default="")
    scope = models.JSONField(default=dict, blank=True)
    status = models.CharField(
        max_length=16, default="running",
        choices=[("running", "Running"), ("completed", "Completed"),
                 ("failed", "Failed"), ("discarded", "Discarded")])
    stage_reports = models.JSONField(default=list, blank=True)
    summary = models.JSONField(default=dict, blank=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                     blank=True, on_delete=models.SET_NULL,
                                     related_name="+")
    started_at = models.DateTimeField(auto_now_add=True)
    finished_at = models.DateTimeField(null=True, blank=True)

    class Meta:
        db_table = "inv_refresh_run"
        ordering = ["-started_at"]

    def __str__(self):
        return f"{self.run_type} · {self.started_at:%Y-%m-%d %H:%M} · {self.status}"

    @property
    def duration_seconds(self):
        if self.finished_at:
            return (self.finished_at - self.started_at).total_seconds()
        return None
