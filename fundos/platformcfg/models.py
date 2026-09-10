"""
Platform configuration — everything the Admin owns from the Django Admin
panel, none of which is exposed as end-user UI.

Design rule (per the clarifications): if a value changes application
behaviour at the user end, it lives here and is editable by an admin
without a code change or a release. Code ships *defaults*; the admin row,
once created, wins.

Contents
  PromptTemplate      — every LLM system/user prompt, per role, versioned
  BrandConfig         — product name, logo, wordmark, export branding
  Country             — seeded master list for the HQ dropdown (C-PRD §5.2)
  FundraiseBucket     — the seven fund-raise values (C9)
  TractionRule        — Outline-driven default bucket selection (C9)
  ProfileSectionConfig— which Company Profile sections exist, what shape each
                        one's data has, and which are mandatory (C2)
  ResearchQuestionBatch/ResearchQuestion
                      — the company-profile research question bank: what the
                        pipeline actually asks the web about a company
  StageDefinition     — stage registry: numbering, labels, prerequisites (CR-03)
  FxRate              — dated USD conversion rates (C11)
  UiCopy              — user-visible strings an admin may reword
"""
import uuid

from django.db import models


# --------------------------------------------------------------------------
# AI prompts — admin-editable, per role
# --------------------------------------------------------------------------
class PromptTemplate(models.Model):
    """One editable prompt per LLM role.

    `llm_generate` resolves the active row for a role; if none exists the
    code default (fundos/llm/default_prompts.py) is used, so the system is
    never broken by an empty table. Admin edits take effect on the next
    call — no restart.

    `context_keys` documents, for the admin, which context variables the
    role receives, so a prompt can be rewritten safely without reading code.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(
        max_length=48, db_index=True,
        help_text="LLM role key, e.g. 'readiness_summary'. Must match the "
                  "role used in code.")
    system_prompt = models.TextField(
        help_text="System message. Describes behaviour, tone and the exact "
                  "JSON shape the model must return. Numbers must never be "
                  "requested from the model — they are injected as context.")
    user_prompt = models.TextField(
        blank=True, default="",
        help_text="User message. Usually a short instruction; the real "
                  "payload travels in the context block.")
    # Point 2 — the admin decides, per prompt, whether this role runs on the
    # tenant's Simple or Advanced LLM. "advanced" == web search. Blank = use
    # the shipped default tier for this role.
    TIERS = (("", "Use shipped default"),
             ("simple", "Simple — extract from supplied text (no tools)"),
             ("advanced", "Advanced / Web — retrieve from the internet"),
             ("judgment", "Judgement — reason over already-extracted data"))
    tier = models.CharField(
        max_length=16, choices=TIERS, blank=True, default="",
        help_text="Which KIND of work this prompt is. SIMPLE: the answer is "
                  "in the supplied context — extraction, no tools, cheapest, "
                  "highest volume. ADVANCED/WEB: the answer is NOT supplied "
                  "and must be found on the internet — web search on, most "
                  "expensive per call, bound it with max_uses. JUDGEMENT: "
                  "reason over data another call already extracted — no "
                  "tools, small input, so the strongest model is affordable "
                  "here. Leave blank to use the shipped default for this "
                  "role. The concrete model per tier is set per tenant under "
                  "LLM → Tenant LLM tiers.")
    purpose = models.TextField(
        blank=True, default="",
        help_text="Plain-English explanation of what this prompt is for and "
                  "when it runs. Shown to admins; seeded from the role's "
                  "shipped notes.")
    context_keys = models.JSONField(
        default=list, blank=True,
        help_text="Reference only — context keys this role receives.")
    notes = models.TextField(
        blank=True, default="",
        help_text="Why this prompt is worded the way it is. For the next admin.")
    version_no = models.IntegerField(default=1)
    is_active = models.BooleanField(default=True, db_index=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "prompt_template"
        verbose_name = "AI Prompt"
        verbose_name_plural = "AI Prompts"
        constraints = [
            models.UniqueConstraint(
                fields=["role"], condition=models.Q(is_active=True),
                name="uq_active_prompt_per_role"),
        ]

    def __str__(self):
        return f"{self.role} (v{self.version_no})"

    @classmethod
    def resolve(cls, role):
        """Active admin row for a role, or None to fall back to code default."""
        try:
            return cls.objects.filter(role=role, is_active=True).first()
        except Exception:
            return None


# --------------------------------------------------------------------------
# Branding (CR-01)
# --------------------------------------------------------------------------
class BrandConfig(models.Model):
    """Singleton product branding. Renaming the product is an admin action.

    Deliberately does NOT rename the Python package or DB tables — only the
    presentation surface, per CR-01.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    product_name = models.CharField(max_length=64, default="Silk")
    legal_name = models.CharField(max_length=128, blank=True, default="")
    tagline = models.CharField(max_length=255, blank=True, default="")
    browser_title = models.CharField(
        max_length=128, blank=True, default="Silk — AI Dealmaking Workspace")
    logo_url = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Full-colour logo (SVG preferred). Used in app chrome.")
    logo_mono_url = models.CharField(max_length=512, blank=True, default="")
    favicon_url = models.CharField(max_length=512, blank=True, default="")
    export_logo_url = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Logo embedded in generated PDF/DOCX/PPTX/XLSX.")
    export_footer_text = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Footer line on every generated document.")
    support_email = models.CharField(max_length=255, blank=True, default="")
    primary_color = models.CharField(max_length=7, blank=True, default="#1F6F4A")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "brand_config"
        verbose_name = "Branding"
        verbose_name_plural = "Branding"

    def __str__(self):
        return self.product_name

    @classmethod
    def get_solo(cls):
        return cls.objects.first()


# --------------------------------------------------------------------------
# Countries (PRD §5.2 — mandatory HQ dropdown)
# --------------------------------------------------------------------------
class Country(models.Model):
    """Seeded country master. `home_currency` drives C11 currency-of-record."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    iso2 = models.CharField(max_length=2, unique=True, db_index=True)
    iso3 = models.CharField(max_length=3, blank=True, default="")
    name = models.CharField(max_length=128, db_index=True)
    dial_code = models.CharField(max_length=8, blank=True, default="")
    home_currency = models.CharField(
        max_length=3, blank=True, default="",
        help_text="Default reporting currency for companies headquartered here.")
    sort_order = models.IntegerField(
        default=100, help_text="Lower sorts first — pin common countries.")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "country_master"
        ordering = ("sort_order", "name")
        verbose_name_plural = "Countries"

    def __str__(self):
        return self.name


# --------------------------------------------------------------------------
# Fund-raise buckets (C9) — the seven values
# --------------------------------------------------------------------------
class FundraiseBucket(models.Model):
    """The seven fund-raise values from C9.

    Per the clarification these are discrete target amounts ($100K, $250K,
    $500K, $1M, $2M, $3M, $5M), not ranges. The traction rules below select
    a default; the founder may override with any other bucket.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    bucket_no = models.IntegerField(unique=True, help_text="1–7, ascending.")
    label = models.CharField(max_length=32, help_text='e.g. "$1M"')
    amount_usd = models.DecimalField(max_digits=20, decimal_places=2)
    typical_stage = models.CharField(max_length=64, blank=True, default="")
    investor_types = models.JSONField(
        default=list, blank=True,
        help_text='Who funds this size, e.g. ["Angels","MicroVC","VC"]. '
                  'From the Outline §3.4.')
    typical_dilution_low_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=10)
    typical_dilution_high_pct = models.DecimalField(
        max_digits=5, decimal_places=2, default=20)
    description = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "fundraise_bucket"
        ordering = ("bucket_no",)
        verbose_name = "Fund-raise Bucket"

    def __str__(self):
        return f"{self.bucket_no}. {self.label}"


class TractionRule(models.Model):
    """Outline-driven default bucket selection (C9).

    Rules are evaluated in `priority` order; the first whose conditions all
    match supplies the recommended bucket. Conditions are expressed as
    admin-editable data so thresholds change without a release.

    Supported condition keys (all optional, all inclusive lower bounds
    unless a _max variant is given):
        arr_usd_min / arr_usd_max
        capital_raised_usd_min / capital_raised_usd_max
        paying_customers_min
        founder_full_time            (bool)
        company_registered           (bool)
        months_full_time_min
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    priority = models.IntegerField(
        default=100, help_text="Lower evaluates first. First match wins.")
    conditions = models.JSONField(
        default=dict, blank=True,
        help_text='e.g. {"arr_usd_min": 500000, "founder_full_time": true}')
    recommended_bucket = models.ForeignKey(
        FundraiseBucket, on_delete=models.PROTECT, related_name="traction_rules")
    rationale = models.TextField(
        blank=True, default="",
        help_text="Shown to the founder as the reason for the recommendation.")
    advice_note = models.TextField(
        blank=True, default="",
        help_text="Extra guidance, e.g. the idea-stage 'come back in 3 months' "
                  "advice from Outline §3.3(a).")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "traction_rule"
        ordering = ("priority",)

    def __str__(self):
        return f"[{self.priority}] {self.name} → {self.recommended_bucket.label}"


class UpliftRule(models.Model):
    """Outline §3.3(g): star founder / exceptional sector adds N bucket levels."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    name = models.CharField(max_length=128)
    condition_key = models.CharField(
        max_length=64,
        help_text="CKB flag that triggers this uplift, e.g. 'star_founder'.")
    levels = models.IntegerField(
        default=2, help_text="Bucket levels to add when triggered.")
    rationale = models.TextField(blank=True, default="")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "uplift_rule"

    def __str__(self):
        return f"{self.name} (+{self.levels})"


# --------------------------------------------------------------------------
# Company Profile sections (C2, CR-07)
# --------------------------------------------------------------------------
class ProfileSectionConfig(models.Model):
    """Which sections the Company Profile has, and which count for
    completeness. C2 notes that sections required for *creating* a profile
    differ from those required for *completeness* — hence two flags.
    """

    KIND = (
        ("structured", "Structured fields"),
        ("narrative", "AI narrative"),
        ("list", "List of records"),
        ("documents", "Document group"),
        ("indicator", "Computed indicator"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    section_key = models.CharField(max_length=64, unique=True, db_index=True)
    label = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    kind = models.CharField(max_length=16, choices=KIND, default="narrative")
    sort_order = models.IntegerField(default=100)
    llm_role = models.CharField(
        max_length=48, blank=True, default="",
        help_text="Role used when regenerating this section.")
    required_for_creation = models.BooleanField(
        default=False,
        help_text="Must be present before a profile can be generated.")
    required_for_completeness = models.BooleanField(
        default=False,
        help_text="Counts toward the 'complete' state that unlocks resume-to-"
                  "last-stage routing (C2).")
    is_editable = models.BooleanField(default=True)
    is_regenerable = models.BooleanField(default=True)
    is_active = models.BooleanField(default=True)

    # --- Output contract --------------------------------------------------
    # These four make the profile's OUTPUT admin-configurable, not just its
    # section list. `field_spec` is rendered directly into the synthesis
    # prompt and read back by the response normalizer, so adding a field here
    # makes the model produce it and the pipeline store it — no deploy.
    #
    # The alternative was a Python constant that had to agree with the prompt,
    # the normalizer and the serializer in four places at once. Single-sourcing
    # it in a table is the same decision the prompt library already made.
    CONTAINER_KINDS = (("object", "Object — data is a single record"),
                       ("array", "Array — data is a list of records"))
    container_kind = models.CharField(
        max_length=8, choices=CONTAINER_KINDS, blank=True, default="",
        help_text="Whether this section's `data` is one object or a list of "
                  "them. Blank uses the shipped default for this section.")
    spec_ref = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Reference shown to the model, e.g. '8.1 Company Overview'. "
                  "Orients it on what depth the section expects.")
    field_spec = models.JSONField(
        default=dict, blank=True,
        help_text="{field name: human-readable type description}. Rendered "
                  "verbatim into the generation prompt — write the "
                  "description as an instruction to the model, e.g. "
                  "'string - ISO-2 country code of headquarters, e.g. IN'. "
                  "Leave empty to use the shipped spec for this section.")
    storage_key = models.CharField(
        max_length=64, blank=True, default="",
        help_text="Internal key this section's data is stored under, when it "
                  "differs from the section key. Set by the seed; changing it "
                  "on a live install orphans existing data.")

    class Meta:
        db_table = "profile_section_config"
        ordering = ("sort_order",)
        verbose_name = "Company Profile Section"

    def __str__(self):
        return f"{self.sort_order}. {self.label}"


# --------------------------------------------------------------------------
# Company-profile research question bank
#
# What the pipeline asks the web about a company. Ten topically coherent
# batches of ten questions: one batch is one search-grounded LLM call, so the
# grouping determines both what the model can chase in a single coherent line
# of research and what the unit of partial failure is.
#
# In a table rather than in code because these questions ARE the product's
# research methodology. An analyst who knows the sector should be able to add
# "what is their GMV take rate" without a release, and should be able to switch
# off six batches when a cheaper pass is wanted. Code ships the 100 defaults;
# the table wins once seeded.
# --------------------------------------------------------------------------
class ResearchQuestionBatch(models.Model):
    """One topic's worth of research questions — one LLM call per batch."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="Stable identifier, e.g. 'funding_investors'. Used by the "
                  "seeder to avoid duplicating a batch it already created.")
    topic = models.CharField(
        max_length=128,
        help_text="Shown to the model and in progress narration, e.g. "
                  "'Funding History & Investors'.")
    covers = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Which profile sections this batch feeds, e.g. '8.10 "
                  "Funding History'. Tells the model what depth to answer at, "
                  "and tells a reader of a partial dossier which sections a "
                  "failed batch left thin.")
    sort_order = models.IntegerField(default=100)
    is_active = models.BooleanField(
        default=True,
        help_text="Deactivate to stop asking this topic. Each batch is one "
                  "billed, search-grounded call, so switching batches off is "
                  "the most direct cost control available.")

    class Meta:
        db_table = "research_question_batch"
        ordering = ("sort_order", "code")
        verbose_name = "Research question batch"
        verbose_name_plural = "Research question batches"

    def __str__(self):
        return f"{self.sort_order}. {self.topic}"


class ResearchQuestion(models.Model):
    """One question inside a batch."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    batch = models.ForeignKey(ResearchQuestionBatch, on_delete=models.CASCADE,
                              related_name="questions")
    text = models.TextField(
        help_text="The question. Use {company_name} and {website} as "
                  "placeholders — they are substituted per company. Any other "
                  "brace expression is sent literally.")
    sort_order = models.IntegerField(default=100)
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "research_question"
        ordering = ("sort_order", "id")

    def __str__(self):
        return self.text[:80]


# --------------------------------------------------------------------------
# Stage registry (CR-03)
# --------------------------------------------------------------------------
class StageDefinition(models.Model):
    """Single source of truth for stage numbering, labels and prerequisites.

    Replaces integers scattered across services, routes and the frontend.
    Per C6 all prerequisites are ADVISORY: they are surfaced, never enforced
    as navigation barriers.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage_no = models.IntegerField(unique=True)
    stage_key = models.CharField(max_length=48, unique=True)
    label = models.CharField(max_length=128)
    purpose = models.TextField(
        blank=True, default="",
        help_text="Shown at the top of the stage page (PRD §2.3).")
    overview = models.TextField(blank=True, default="")
    prerequisites = models.JSONField(
        default=list, blank=True,
        help_text='[{"key","label","explanation"}] — advisory only (C6).')
    next_actions = models.JSONField(default=list, blank=True)
    is_implemented = models.BooleanField(
        default=False, help_text="False renders a 'coming soon' state.")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "stage_definition"
        ordering = ("stage_no",)
        verbose_name = "Stage"

    def __str__(self):
        return f"{self.stage_no}. {self.label}"


# --------------------------------------------------------------------------
# FX (C11)
# --------------------------------------------------------------------------
class FxRate(models.Model):
    """Dated FX rates. C11 requires the rate used to be stored with each
    artefact, so rates are dated rows rather than a mutable constant."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    base_ccy = models.CharField(max_length=3, default="USD")
    quote_ccy = models.CharField(max_length=3)
    rate = models.DecimalField(
        max_digits=20, decimal_places=6,
        help_text="Units of quote per 1 unit of base. USD→INR ≈ 84.")
    as_of_date = models.DateField(db_index=True)
    source = models.CharField(
        max_length=128, blank=True, default="manual",
        help_text="Provider or 'manual'. Recorded on every artefact.")

    class Meta:
        db_table = "fx_rate"
        unique_together = [("base_ccy", "quote_ccy", "as_of_date")]
        ordering = ("-as_of_date",)

    def __str__(self):
        return f"{self.base_ccy}/{self.quote_ccy} {self.rate} @ {self.as_of_date}"

    @classmethod
    def latest(cls, base="USD", quote="INR"):
        return cls.objects.filter(base_ccy=base, quote_ccy=quote).first()


# --------------------------------------------------------------------------
# Editable UI copy
# --------------------------------------------------------------------------
class UiCopy(models.Model):
    """User-visible strings an admin may reword without a release —
    empty-state messages, advisory notes, disclaimers."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    key = models.CharField(max_length=96, unique=True, db_index=True)
    text = models.TextField()
    description = models.CharField(
        max_length=255, blank=True, default="",
        help_text="Where this appears in the product.")
    is_active = models.BooleanField(default=True)

    class Meta:
        db_table = "ui_copy"
        ordering = ("key",)
        verbose_name = "UI Copy"
        verbose_name_plural = "UI Copy"

    def __str__(self):
        return self.key

    @classmethod
    def get(cls, key, default=""):
        row = cls.objects.filter(key=key, is_active=True).first()
        return row.text if row else default


# --------------------------------------------------------------------------
# One-time repair markers (v29)
# --------------------------------------------------------------------------
class PlatformFlag(models.Model):
    """A named, idempotent 'this has been done once' marker.

    WHY THIS EXISTS
    ---------------
    Seed repairs come in two kinds and v28 conflated them:

      * INVARIANTS — re-applied on every run, because the correct value is a
        property of the system and an administrator cannot legitimately want
        otherwise (e.g. a searching tier must carry a search cap).
      * ALIGNMENTS — applied once to bring an existing install onto a new
        default, after which the administrator's choice is theirs to make.

    v28 implemented the structured-output/search alignment as an invariant:
    an unguarded `.update()` on every seed run. That made the setting
    permanently unsettable — an admin could change it, and the next deploy
    would silently change it back — which is how a diagnostic becomes
    something operators route around rather than read.

    A flag row is the difference between the two. Deliberately dumb: a key, a
    timestamp, and no value. If a repair needs to run again on purpose,
    delete the row.
    """
    key = models.CharField(max_length=128, primary_key=True)
    set_at = models.DateTimeField(auto_now_add=True)
    note = models.CharField(max_length=255, blank=True, default="")

    class Meta:
        db_table = "platform_flag"
        ordering = ("key",)

    def __str__(self):
        return self.key

    @classmethod
    def is_set(cls, key):
        """True when this repair has already run. Never raises.

        Returns False if the table is not migrated yet, so a fresh install
        performs the alignment rather than skipping it on a missing table.
        """
        try:
            return cls.objects.filter(key=key).exists()
        except Exception:
            return False

    @classmethod
    def set(cls, key, note=""):
        try:
            cls.objects.get_or_create(key=key, defaults={"note": note})
        except Exception:
            pass
