"""
Company Profile (PRD §5.4).

Scoping note (C1): a Company is an independent entity that may exist with
no deal, and may have several concurrent deals (an equity raise and a debt
raise at the same time). The profile therefore hangs off the COMPANY, not
off a deal — this is the key structural difference from the previous
Stage-1 model, where everything was deal-scoped.

Provenance is preserved on every generated field so an advisor can still
see where a value came from and whether a human verified it.
"""
import uuid

from django.conf import settings
from django.db import models

from fundos.core.models.base import BaseModel, ProvenanceMixin, VersionedMixin


class CompanyProfile(BaseModel):
    """One per company. The container for all profile sections."""

    STATUS = (
        ("draft", "draft"),
        ("generating", "generating"),
        ("generated", "generated"),
        ("reviewed", "reviewed"),
    )

    company = models.OneToOneField(
        "core.Company", on_delete=models.CASCADE, related_name="profile")

    # Onboarding inputs (PRD §5.2)
    website_url = models.CharField(max_length=512, blank=True, default="")
    # The COMPANY's own LinkedIn page (CR-01). Distinct from Founder.
    # linkedin_url and KeyPerson.linkedin_url, which are personal profiles.
    # Without this the dashboard's LinkedIn shortcut had nothing company-level
    # to point at and fell back to the first founder's personal profile.
    linkedin_url = models.CharField(max_length=512, blank=True, default="")
    hq_country = models.CharField(
        max_length=2, blank=True, default="",
        help_text="ISO alpha-2, resolved against the countries master.")
    home_currency = models.CharField(
        max_length=3, blank=True, default="",
        help_text="C11 — currency of record for this company.")
    # Company logo — either a direct URL (e.g. logo.dev) stored as-is, or the
    # URL of a base64 upload saved to object storage. Surfaced at the top level
    # of the profile response and on me/contexts company items.
    logo_url = models.CharField(max_length=1024, blank=True, default="")

    status = models.CharField(max_length=16, choices=STATUS, default="draft")
    completeness_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                           default=0)

    # C2: completeness = mandatory sections populated + owner confirms review
    reviewed_at = models.DateTimeField(null=True, blank=True)
    reviewed_by = models.ForeignKey(
        settings.AUTH_USER_MODEL, null=True, blank=True,
        on_delete=models.SET_NULL, related_name="+")

    generation_job_id = models.UUIDField(null=True, blank=True)
    last_generated_at = models.DateTimeField(null=True, blank=True)

    # Cost control: fingerprint of the source bundle that produced the last
    # successful generation. When the sources have not moved, regeneration is
    # skipped entirely rather than paying for a byte-identical result.
    sources_hash = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="SHA-256 of the collected sources at last generation.")

    # Conclusions from the Judgement-tier call: which value won each
    # conflict, the identified risks, the leadership assessment. Persisted so
    # every downstream Simple call can reuse the expensive reasoning instead
    # of re-deriving (and potentially contradicting) it.
    judgment = models.JSONField(
        default=dict, blank=True,
        help_text="Distilled output of the Judgement-tier call. Reused as "
                  "authoritative context by cheaper calls.")

    class Meta:
        db_table = "company_profile"

    def __str__(self):
        return f"Profile · {self.company_id}"

    @property
    def is_complete(self):
        """C2 — all mandatory sections populated AND review confirmed."""
        if not self.reviewed_at:
            return False
        from fundos.platformcfg.services import completeness_section_keys
        required = set(completeness_section_keys())
        if not required:
            return True
        filled = set(
            ProfileSection.objects.filter(
                profile=self, section_key__in=required, is_active=True)
            .exclude(content="").exclude(content__isnull=True)
            .values_list("section_key", flat=True))
        return required.issubset(filled)


class ProfileSection(BaseModel, VersionedMixin, ProvenanceMixin):
    """One editable section of the profile.

    A uniform contract across every section — read, update, regenerate,
    version history — rather than bespoke handling per section. Adding a
    section is an admin action (ProfileSectionConfig), not a code change.
    """

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="sections")
    section_key = models.CharField(max_length=64, db_index=True)
    content = models.TextField(blank=True, default="")
    structured = models.JSONField(
        default=dict, blank=True,
        help_text="For structured sections — field/value pairs.")
    needs_input = models.BooleanField(
        default=False,
        help_text="Generation produced too little; the founder should complete it.")
    missing_notes = models.JSONField(default=list, blank=True)
    edited_by_user = models.BooleanField(
        default=False,
        help_text="True once a human edits it — regeneration then warns first.")
    confirmed_fields = models.JSONField(
        default=list, blank=True,
        help_text="Field keys the founder has explicitly confirmed "
                  "(AI Draft → Confirmed). Used by the readiness scorer.")

    # Where each value in this section came from, keyed the same way a
    # confirmation is: a field name for an object section, an item id for a
    # list one. So the thing a founder is asked to confirm and the evidence
    # they are shown to confirm it by are addressed identically, and a panel
    # can fetch one from the other without a mapping table.
    #
    # Recorded at synthesis time because synthesis is the only party that
    # knows. It reads the dossier and writes the value; nothing downstream can
    # recover which of forty pages a sentence came from without guessing, and
    # a guessed citation is worse than none — it survives review by looking
    # exactly like a real one.
    field_sources = models.JSONField(
        default=dict, blank=True,
        help_text="Per-field provenance: {field_or_item_id: {source, "
                  "locator, quote}}. Served by the field sources endpoint.")

    # Cost control: which source bundle produced this section. Lets a
    # regeneration re-do only the sections whose inputs actually changed.
    source_hash = models.CharField(
        max_length=64, blank=True, default="",
        help_text="SHA-256 of the sources used to generate this section.")

    class Meta:
        db_table = "company_profile_section"
        indexes = [models.Index(fields=["profile", "section_key"])]
        constraints = [
            models.UniqueConstraint(
                fields=["profile", "section_key"],
                condition=models.Q(is_active=True, is_deleted=False),
                name="uq_active_profile_section"),
        ]

    def __str__(self):
        return f"{self.section_key} v{self.version_no}"


class ProfileSectionVersion(models.Model):
    """Append-only history for section rollback (PRD §5.4)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(db_index=True)
    section = models.ForeignKey(ProfileSection, on_delete=models.CASCADE,
                                related_name="history")
    version_no = models.IntegerField()
    content = models.TextField(blank=True, default="")
    structured = models.JSONField(default=dict, blank=True)
    change_summary = models.CharField(max_length=255, blank=True, default="")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                   blank=True, on_delete=models.SET_NULL,
                                   related_name="+")
    changed_at = models.DateTimeField(auto_now_add=True)
    ai_model_version = models.CharField(max_length=128, blank=True, default="")

    class Meta:
        db_table = "company_profile_section_version"
        ordering = ("-version_no",)


class Founder(BaseModel, ProvenanceMixin):
    """Repeatable founder record (PRD §5.2 — no limit on count).

    C3: LinkedIn is optional. When supplied it is processed from the public
    profile only, and the founder confirms the result as their own input —
    `self_confirmed` records that confirmation.
    """

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="founders")
    name = models.CharField(max_length=255, blank=True, default="")
    designation = models.CharField(max_length=128, blank=True, default="")
    linkedin_url = models.CharField(max_length=512, blank=True, default="")
    experience = models.TextField(blank=True, default="")
    education = models.TextField(blank=True, default="")
    previous_companies = models.JSONField(default=list, blank=True)
    biography = models.TextField(blank=True, default="")
    is_full_time = models.BooleanField(
        default=True,
        help_text="Feeds the traction rules that pick a fund-raise bucket.")
    months_full_time = models.IntegerField(null=True, blank=True)
    self_confirmed = models.BooleanField(
        default=False,
        help_text="C3 — founder confirmed reviewing this as their own input.")
    self_confirmed_at = models.DateTimeField(null=True, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_founder"
        ordering = ("sort_order",)

    def __str__(self):
        return self.name or self.linkedin_url or "Founder"


class KeyPerson(BaseModel, ProvenanceMixin):
    """Leadership beyond the founders (PRD §5.4 Key People)."""

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="key_people")
    name = models.CharField(max_length=255)
    designation = models.CharField(max_length=128, blank=True, default="")
    linkedin_url = models.CharField(max_length=512, blank=True, default="")
    biography = models.TextField(blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_key_person"
        ordering = ("sort_order",)


class Competitor(BaseModel, ProvenanceMixin):
    """AI-discovered competitor card, editable (PRD §5.4)."""

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="competitors")
    name = models.CharField(max_length=255)
    website = models.CharField(max_length=512, blank=True, default="")
    description = models.TextField(blank=True, default="")
    positioning = models.TextField(blank=True, default="")
    geography = models.CharField(max_length=128, blank=True, default="")
    funding_raised_usd = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True)
    # §7.12 competitor benchmark fields. Previously these had no columns, so
    # PATCH silently dropped fy_year / revenue / investors (backend-issues
    # combined report #2). Now first-class, persisted and round-tripped.
    fy_year = models.IntegerField(null=True, blank=True)
    revenue = models.DecimalField(max_digits=20, decimal_places=2,
                                  null=True, blank=True)
    investors = models.JSONField(default=list, blank=True)
    # Req 8 competitor-analysis fields (business_model, market_positioning,
    # latest_valuation_usd_mn, revenue_growth_pct, market_share_pct,
    # relative_scale, key_differentiators, strengths, weaknesses,
    # ev_revenue_multiple, ev_ebitda_multiple, recent_activity).
    #
    # One JSON column rather than eleven sparse ones: these are display-only
    # analysis attributes that the frontend and the AI both treat as a bag,
    # none of them is queried or joined on, and several are lists. Eleven
    # columns would buy nothing and cost a migration each time the analysis
    # set changes.
    extra = models.JSONField(default=dict, blank=True)
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_competitor"
        ordering = ("sort_order",)


class FundingRound(BaseModel, ProvenanceMixin):
    """Historical funding event (PRD §5.4 Funding History).

    Distinct from `Deal`: this records rounds the company has ALREADY
    raised, whereas a Deal is a raise in progress.
    """

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="funding_rounds")
    round_name = models.CharField(max_length=64, blank=True, default="")
    announced_date = models.DateField(null=True, blank=True)
    # Rounds are routinely announced with month precision, and the month IS
    # the fact. The date is stored on the first of the period and this says
    # how much of it was actually known, so the UI can render "May 2006"
    # instead of asserting a day nobody reported.
    DATE_PRECISION = (("", "Unknown"), ("day", "Exact date"),
                      ("month", "Month"), ("year", "Year"))
    date_precision = models.CharField(max_length=5, blank=True, default="",
                                      choices=DATE_PRECISION)
    # --- the amount, as the source stated it -----------------------------
    #
    # `amount_value` + `amount_ccy` are the fact. What was missing was the
    # SCALE: a round the deck states as "INR 20 crore" had nowhere to record
    # "crore", so the model converted it to USD millions before writing —
    # at a rate it decided in a sentence ("approximately 83 INR to 1 USD")
    # and then discarded. What reached the row was `2.4`. The deck's own
    # words were gone, the rate was unauditable, and 12 crore was rounded to
    # $1.6M when it is $1.45M, putting a scorecard row 10% low.
    #
    # The USD companion below is DERIVED and says so, with the rate and its
    # date, so a wrong rate is a correctable error rather than a permanent
    # one.
    amount_value = models.DecimalField(max_digits=20, decimal_places=2,
                                       null=True, blank=True)
    amount_ccy = models.CharField(max_length=3, blank=True, default="USD")
    amount_denomination = models.CharField(
        max_length=4, blank=True, default="",
        help_text="Scale as written: Cr, L, K, Mn, Bn, or blank for whole "
                  "units. Crore and lakh are kept because that is how the "
                  "figure was reported, and converting at storage makes it "
                  "unrecognisable to whoever wrote it.")
    amount_usd_mn = models.DecimalField(
        max_digits=20, decimal_places=4, null=True, blank=True,
        help_text="DERIVED from the three fields above for comparison and "
                  "sorting. Never authoritative; blank when no rate applies.")
    fx_rate = models.DecimalField(
        max_digits=12, decimal_places=4, null=True, blank=True,
        help_text="Units of amount_ccy per USD used for amount_usd_mn.")
    fx_as_of = models.DateField(
        null=True, blank=True,
        help_text="When that rate was struck.")
    amount_basis = models.CharField(max_length=32, blank=True, default="actual")
    pre_money_value = models.DecimalField(max_digits=20, decimal_places=2,
                                          null=True, blank=True)
    # Req 1: the subset of `investors` who led the round. Stored separately
    # rather than flagged inside `investors` so the existing array shape is
    # untouched for clients already reading it.
    lead_investors = models.JSONField(default=list, blank=True)
    post_money_value = models.DecimalField(max_digits=20, decimal_places=2,
                                           null=True, blank=True)
    valuation_ccy = models.CharField(max_length=3, blank=True, default="USD")
    investors = models.JSONField(default=list, blank=True)
    lead_investor = models.CharField(max_length=255, blank=True, default="")
    notes = models.TextField(blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_funding_round"
        ordering = ("-announced_date", "sort_order")


class NewsItem(BaseModel, ProvenanceMixin):
    """Recent news (PRD §5.4)."""

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="news")
    headline = models.CharField(max_length=512)
    summary = models.TextField(blank=True, default="")
    url = models.CharField(max_length=1024, blank=True, default="")
    published_date = models.DateField(null=True, blank=True)
    publisher = models.CharField(max_length=255, blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_news"
        ordering = ("-published_date", "sort_order")


class ProfileDocument(BaseModel):
    """Document Center grouping (PRD §5.4).

    Categories match the PRD exactly. C7: artefacts generated by the old
    Stage 3 are migrated here as read-only addenda linked to the company,
    pending their rebuild as assistants.
    """

    CATEGORY = (
        ("company_presentation", "Company Presentation"),
        ("financial_model", "Financial Model"),
        ("annual_report", "Annual Report / Financial Statements"),
        ("other", "Other Documents"),
        # C7 — migrated Stage-3 output, read-only for now
        ("teaser", "Teaser"),
        ("pitch_deck", "Pitch Deck"),
        ("information_memorandum", "Information Memorandum"),
    )

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="documents")
    document_id = models.UUIDField(null=True, blank=True, db_index=True)
    material_asset_id = models.UUIDField(null=True, blank=True, db_index=True)
    category = models.CharField(max_length=32, choices=CATEGORY, default="other")
    filename = models.CharField(max_length=255, blank=True, default="")
    is_readonly = models.BooleanField(
        default=False,
        help_text="C7 — migrated Stage-3 artefacts are read-only until the "
                  "corresponding assistant is rebuilt.")
    origin_stage = models.CharField(max_length=32, blank=True, default="")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_profile_document"
        ordering = ("category", "sort_order")


class CashPosition(BaseModel):
    """Stage 2 Step 2 — cash & runway (PRD §6; C5).

    C5: burn is an INPUT (average monthly net cash burn), not a derived
    field, and may be zero or negative for a profitable company. Runway is
    derived from cash ÷ burn and is only meaningful when burn is positive.
    """

    company = models.ForeignKey("core.Company", on_delete=models.CASCADE,
                                related_name="cash_positions")
    deal_id = models.UUIDField(null=True, blank=True, db_index=True)

    cash_in_bank_value = models.DecimalField(max_digits=20, decimal_places=2,
                                             null=True, blank=True)
    cash_in_bank_ccy = models.CharField(max_length=3, default="USD")
    monthly_burn_value = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True,
        help_text="Average monthly NET cash burn. Zero or negative if the "
                  "company is profitable.")
    monthly_burn_ccy = models.CharField(max_length=3, default="USD")
    as_of_date = models.DateField(
        null=True, blank=True,
        help_text="Date the cash balance was accurate — the exhaustion date "
                  "is calculated from here.")
    is_profitable = models.BooleanField(default=False)

    # Derived (computed deterministically, stored for audit)
    runway_months = models.DecimalField(max_digits=8, decimal_places=2,
                                        null=True, blank=True)
    cash_exhaustion_date = models.DateField(null=True, blank=True)
    additional_capital_required_usd = models.DecimalField(
        max_digits=20, decimal_places=2, null=True, blank=True)
    fx_rate_used = models.DecimalField(max_digits=20, decimal_places=6,
                                       null=True, blank=True)
    fx_as_of = models.DateField(null=True, blank=True)
    is_active = models.BooleanField(default=True, db_index=True)

    class Meta:
        db_table = "company_cash_position"

    def __str__(self):
        return f"Cash · {self.company_id}"


class Investor(BaseModel, ProvenanceMixin):
    """Cap-table / investor-list entry (Company Profile requirement §2).

    Captures who has invested, their type, which rounds they joined, and an
    optional ownership percentage for the cap-table view. Founders and ESOP
    are represented as rows with holder_category set accordingly.
    """

    TYPES = (
        ("VC", "Venture Capital"),
        ("PE", "Private Equity"),
        ("Angel", "Angel"),
        ("Strategic", "Strategic"),
        ("FamilyOffice", "Family Office"),
        ("Other", "Other"),
    )

    profile = models.ForeignKey(CompanyProfile, on_delete=models.CASCADE,
                                related_name="investors")
    name = models.CharField(max_length=255)
    investor_type = models.CharField(max_length=16, choices=TYPES, blank=True,
                                     default="Other")
    rounds = models.JSONField(
        default=list, blank=True,
        help_text="Round names this investor participated in.")
    ownership_pct = models.DecimalField(max_digits=6, decimal_places=3,
                                        null=True, blank=True)
    holder_category = models.CharField(
        max_length=16, blank=True, default="Investor",
        help_text="Founder | Investor | ESOP | Other — drives the cap-table "
                  "split.")
    sort_order = models.IntegerField(default=0)

    class Meta:
        db_table = "company_investor"
        ordering = ("sort_order",)

    def __str__(self):
        return self.name or "Investor"


# --------------------------------------------------------------------------
# Manual deal targets (C6) — imported so Django registers the model with
# this app. Kept in its own module because the calculation logic is
# substantial enough to warrant separation.
# --------------------------------------------------------------------------
from fundos.profile.targets import DealTargets  # noqa: E402,F401

# --------------------------------------------------------------------------
# Phase 3 — typed assessment inputs emitted by step 1 research, under the
# assessment workbook's own input_key names. Imported here so Django
# registers the model with this app; the logic lives in its own module
# because per-field provenance is substantial enough to warrant separation.
# --------------------------------------------------------------------------
from fundos.profile.assessment_inputs import (  # noqa: E402,F401
    ProfileAssessmentInput)


class ProfileGenerationRun(models.Model):
    """One diagnostic record per Company Profile generation.

    LLMCallLog answers "what did each call cost?". This answers the questions
    you need to IMPROVE the pipeline, none of which are visible per-call:

      * Which sources were available, and how big were they? Output quality
        is mostly a function of input quality, and without this you cannot
        tell a bad prompt from a thin website.
      * Which sources FAILED? A silently failing research adapter looks
        identical to a company with no public data.
      * Which sections came back needing input, run over run? A section that
        is always empty is a pipeline bug, not a sourcing gap.
      * Did the judgement stage run, and did it resolve anything?
      * What did the whole run cost, and how many calls did it take?

    Written once per run and never updated, so a series of rows is a time
    series you can regress against prompt and model changes.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    profile = models.ForeignKey("companyprofile.CompanyProfile",
                                on_delete=models.CASCADE,
                                related_name="generation_runs")
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    triggered_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True,
                                     blank=True, on_delete=models.SET_NULL,
                                     related_name="+")

    mode = models.CharField(
        max_length=24, blank=True, default="",
        help_text="consolidated / per_section / skipped")
    forced = models.BooleanField(default=False)
    sources_hash = models.CharField(max_length=64, blank=True, default="")

    # --- Inputs: what the model actually had to work with ---------------
    source_stats = models.JSONField(
        default=dict, blank=True,
        help_text="Per-source presence and size, e.g. "
                  "{'website_chars': 4200, 'document_count': 3, "
                  "'document_chars': 8100, 'research_keys': ['market'], "
                  "'founder_count': 2}. The denominator for every quality "
                  "question about this run.")
    source_failures = models.JSONField(
        default=list, blank=True,
        help_text="Adapters that failed. A silently failing source looks "
                  "exactly like a company with no public data.")

    # --- Outputs: what came back ----------------------------------------
    sections_generated = models.JSONField(default=list, blank=True)
    sections_skipped = models.JSONField(default=list, blank=True)
    sections_needing_input = models.JSONField(
        default=list, blank=True,
        help_text="Sections still incomplete after the run. Track these "
                  "across runs: one that is ALWAYS empty is a bug, one that "
                  "varies with sources is working correctly.")
    completeness_pct = models.DecimalField(max_digits=5, decimal_places=2,
                                           default=0)

    # --- Judgement stage -------------------------------------------------
    judgment_ran = models.BooleanField(default=False)
    conflicts_found = models.IntegerField(
        default=0,
        help_text="Conflicts the extraction stage surfaced. Zero on a "
                  "company with messy public data usually means the model "
                  "silently picked — a quality FAILURE that looks like "
                  "success.")
    conflicts_resolved = models.IntegerField(default=0)

    # --- Economics -------------------------------------------------------
    llm_calls = models.IntegerField(default=0)
    total_cost_inr = models.DecimalField(max_digits=14, decimal_places=6,
                                         default=0)
    total_search_count = models.IntegerField(default=0)
    duration_ms = models.IntegerField(default=0)
    error = models.TextField(blank=True, default="")

    # --- Live progress ----------------------------------------------------
    # The fields above are a post-mortem, written once the run is over. These
    # answer the question a founder actually asks while waiting — "is this
    # working, and how much longer?" — which a terminal-state-only record
    # cannot. A run that takes twenty minutes and reports nothing until minute
    # twenty is indistinguishable from a wedged one.
    STATUS = (("queued", "queued"), ("running", "running"),
              ("succeeded", "succeeded"), ("failed", "failed"),
              ("refused", "refused"))
    status = models.CharField(max_length=12, choices=STATUS, default="queued",
                              db_index=True)
    stage = models.CharField(
        max_length=32, blank=True, default="queued",
        help_text="Current pipeline stage: researching / processing_documents "
                  "/ consolidating / synthesizing / writing / completed.")
    stage_timings = models.JSONField(
        default=dict, blank=True,
        help_text="Per-stage {state, started_at, finished_at, "
                  "duration_seconds}. Says WHICH stage is eating the wall "
                  "clock, which the total alone never does.")
    started_at = models.DateTimeField(null=True, blank=True)
    finished_at = models.DateTimeField(null=True, blank=True)
    progress = models.JSONField(
        default=dict, blank=True,
        help_text="Per-source result summaries, keyed by stage.")

    # --- Source 1: research ----------------------------------------------
    batches_total = models.IntegerField(default=0)
    batches_succeeded = models.IntegerField(default=0)
    batches_failed = models.JSONField(
        default=list, blank=True,
        help_text="Indices of research batches that returned no data. A batch "
                  "is one LLM call over one topic, so this names exactly "
                  "which subject areas the profile is thin on.")

    # --- The evidence ------------------------------------------------------
    dossier_uri = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Storage path of this run's consolidated.md — the merged "
                  "source material the profile was derived from. Served "
                  "through a short-lived signed URL.")
    dossier_chars = models.IntegerField(
        default=0,
        help_text="Size of the dossier. A tiny one means the sources found "
                  "nothing, which is what explains a thin profile.")

    # --- Source 2: documents ----------------------------------------------
    documents = models.JSONField(
        default=list, blank=True,
        help_text="Per-file extraction outcome: handler chosen, OCR status "
                  "and reason, characters recovered. Answers 'was my deck "
                  "actually read?' without reading logs.")

    class Meta:
        db_table = "profile_generation_run"
        indexes = [
            models.Index(fields=["profile", "-created_at"]),
            models.Index(fields=["tenant_id", "-created_at"]),
        ]

    def __str__(self):
        return f"{self.profile_id} · {self.mode} · {self.created_at:%Y-%m-%d}"


class ProfileRunEvent(models.Model):
    """One line in a generation run's activity log.

    A stage timing says a stage took eleven minutes; this says what it was
    doing for them — "research batch 4/10 'Financial Performance' ok after
    41.2s", "OCR triggered on page 12/40 (only 8 chars of native text)". That
    narration is the difference between a long-running job a founder trusts
    and one they assume has hung.

    Append-only and bounded (see ``pipeline.tracker.MAX_EVENTS``): this
    explains what just happened, it is not an audit trail. AuditLog is the
    audit trail.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    run = models.ForeignKey(ProfileGenerationRun, on_delete=models.CASCADE,
                            related_name="events")
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    at = models.DateTimeField(auto_now_add=True, db_index=True)
    stage = models.CharField(max_length=32, blank=True, default="")
    message = models.TextField(blank=True, default="")
    t_plus_seconds = models.FloatField(
        null=True, blank=True,
        help_text="Seconds since the run started — easier to scan than "
                  "absolute timestamps when reading one run's log.")
    detail = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "profile_run_event"
        ordering = ("at", "id")
        indexes = [models.Index(fields=["run", "at"],
                                name="profile_run_event_run_at_idx")]

    def __str__(self):
        return f"{self.stage}: {self.message[:60]}"
