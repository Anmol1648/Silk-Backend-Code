"""
Configuration & master data (Doc 2 M0.6, Doc 6 §1).

AppConfiguration — the Gyain settings-spine pattern: a singleton row read
lazily at call time (never cached at import). Secrets stay OUT of the DB —
env-var *names* travel, values don't.
"""
import os
import uuid

from django.conf import settings
from django.db import models


class AppConfiguration(models.Model):
    """Singleton runtime-editable application settings (Doc 6 §1)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    # General
    features = models.JSONField(default=dict, blank=True,
                                help_text="Free-form feature block (Doc 6 §2 flags).")

    # Email
    smtp_host = models.CharField(max_length=255, blank=True, default="")
    smtp_port = models.IntegerField(null=True, blank=True)
    smtp_user = models.CharField(max_length=255, blank=True, default="")
    smtp_password_env_var = models.CharField(
        max_length=128, blank=True, default="FUNDOS_SMTP_PASSWORD",
        help_text="ENV VAR NAME holding the password — value never in DB.")
    from_email = models.CharField(max_length=255, blank=True, default="")
    reply_to = models.CharField(max_length=255, blank=True, default="")

    # OTP / login
    otp_option = models.CharField(
        max_length=16, default="email",
        choices=(("email", "email"), ("whatsapp", "whatsapp")))

    # WhatsApp (Meta Cloud API)
    whatsapp_phone_number_id = models.CharField(max_length=64, blank=True, default="")
    whatsapp_access_token = models.TextField(
        blank=True, default="",
        help_text="Prefer whatsapp_access_token_env_var in FundOS (Doc 6 §1).")
    whatsapp_access_token_env_var = models.CharField(
        max_length=128, blank=True, default="FUNDOS_WHATSAPP_TOKEN")
    whatsapp_otp_template_name = models.CharField(max_length=128, blank=True, default="")
    whatsapp_alert_template_name = models.CharField(
        max_length=128, blank=True, default="alert_generic")
    whatsapp_alert_image_template_name = models.CharField(
        max_length=128, blank=True, default="alert_generic_image")
    whatsapp_otp_template_lang = models.CharField(max_length=8, blank=True, default="en")

    # AI
    ai_mocked = models.BooleanField(
        default=True, help_text="Global mock switch for all LLM roles.")
    ai_guidance_data = models.TextField(blank=True, default="")
    ai_guidance_documents = models.TextField(blank=True, default="")
    ai_synthetic_disclosure_enabled = models.BooleanField(
        default=True, help_text='Forces "AI Generated Insights" labelling on.')

    # FundOS-specific
    show_numeric_readiness = models.BooleanField(default=False)
    enable_research_extensions = models.BooleanField(default=False)
    enable_instrument_selection = models.BooleanField(default=True)
    enable_dilution_simulator = models.BooleanField(default=True)
    scoring_config_active_version = models.IntegerField(null=True, blank=True)
    hard_gate_config_version = models.IntegerField(null=True, blank=True)
    sector_taxonomy_map_version = models.IntegerField(null=True, blank=True)
    investor_category_map_version = models.IntegerField(null=True, blank=True)
    mandatory_ckb_fields = models.JSONField(
        default=list, blank=True,
        help_text="Field keys counted for Stage-1 completeness (BR-M1-000).")

    # ---------------------------------------------------------------
    # Company Profile pipeline
    #
    # Real columns rather than entries in the `features` blob: these are the
    # dials an operator reaches for when a run is too slow, too expensive or
    # producing thin documents, and a labelled field with help text and the
    # right widget is the difference between a setting someone can find and
    # one they have to be told about.
    # ---------------------------------------------------------------
    profile_research_concurrency = models.IntegerField(
        default=5,
        help_text="How many research batches run at once (1-10). Each batch "
                  "is one search-grounded LLM call. Raising this shortens a "
                  "run without changing its cost; it is bounded by the "
                  "provider's per-minute rate limit, not by us. Ignored on "
                  "SQLite, which cannot take concurrent writers — the run "
                  "log says so when it clamps.")
    profile_ocr_enabled = models.BooleanField(
        default=True,
        verbose_name="Read documents with a model",
        help_text="Read scanned pages and the text inside deck images by "
                  "sending the FILE to a document-capable model, in place of "
                  "a local OCR engine. Safe to leave on: a file the model "
                  "cannot read still keeps whatever native extraction "
                  "recovered, and the dossier records why. Turn off to skip "
                  "the token cost, at the price of anything that exists only "
                  "as pixels.")
    profile_document_timeout_s = models.IntegerField(
        default=600,
        help_text="Per-file deadline for OCR, in seconds. Native extraction "
                  "is unaffected.")
    profile_research_config_profile = models.CharField(
        max_length=64, blank=True, default="profile.research",
        help_text="LLM config profile code for the research calls (web "
                  "search ON). Edit that profile under LLM -> Config "
                  "profiles to change the model for every research call.")
    profile_synthesis_config_profile = models.CharField(
        max_length=64, blank=True, default="profile.synthesis",
        help_text="LLM config profile code for the single synthesis call "
                  "(web search OFF).")
    profile_max_dossier_chars = models.IntegerField(
        default=600000,
        help_text="Ceiling on the dossier text sent to the synthesis call. A "
                  "runaway guard, not a routine truncation — when it bites, "
                  "the dossier says so.")

    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "app_configuration"
        verbose_name = "Application Settings"

    @classmethod
    def get_solo(cls):
        """Access pattern per Doc 6 §1 — resolve lazily at call time."""
        return cls.objects.first()

    @property
    def whatsapp_access_token_value(self):
        """Env-var name preferred; DB value as legacy fallback."""
        if self.whatsapp_access_token_env_var:
            val = os.environ.get(self.whatsapp_access_token_env_var, "")
            if val:
                return val
        return self.whatsapp_access_token or None


class ScoringConfig(models.Model):
    """Versioned scoring config — the single most important config object
    (Doc 2 M0.6). Engines read the active version; every generated report
    persists the scoring_config version id it used."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    version_no = models.IntegerField()
    is_active = models.BooleanField(default=False, db_index=True)
    data = models.JSONField(default=dict)
    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                   on_delete=models.SET_NULL, related_name="+")
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        db_table = "scoring_config"
        unique_together = [("version_no",)]

    @classmethod
    def active(cls):
        return cls.objects.filter(is_active=True).order_by("-version_no").first()


class ResearchSourceFlags(models.Model):
    """Per-adapter enable/disable for Pillar 1 (Doc 2 M0.6 / BR-M1-003)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    source_type = models.CharField(max_length=32, unique=True)
    enabled = models.BooleanField(default=True)
    legal_cleared = models.BooleanField(default=False)
    notes = models.TextField(blank=True, default="")

    class Meta:
        db_table = "research_source_flags"


class StageConfig(models.Model):
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage_no = models.SmallIntegerField(unique=True)
    name = models.CharField(max_length=128)
    soft_dependencies = models.JSONField(default=list, blank=True)
    estimated_duration_days = models.IntegerField(default=0)
    optional_flags = models.JSONField(default=dict, blank=True)

    class Meta:
        db_table = "stage_config"


class HardGateConfig(models.Model):
    """Versioned hard-gate allowlist (BR-M0-031)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    stage_no = models.SmallIntegerField()
    precondition_key = models.CharField(max_length=128)
    description = models.TextField(blank=True, default="")
    version_no = models.IntegerField(default=1)

    class Meta:
        db_table = "hard_gate_config"


class MasterValue(models.Model):
    """Generic admin-owned master/reference values (Doc 2 M0.6 table set:
    m_round_type, m_investor_category, m_sector, m_subsector, m_purpose,
    m_runway_band, m_timeline, m_dilution_appetite, m_valuation_method,
    m_instrument, m_business_model, m_doc_template, m_kpi,
    m_document_status, m_readiness_dimension)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    table = models.CharField(max_length=48, db_index=True)   # e.g. m_round_type
    code = models.CharField(max_length=64)
    label = models.CharField(max_length=255, blank=True, default="")
    meta = models.JSONField(default=dict, blank=True)         # mechanics text, enable flags…
    sort_order = models.IntegerField(default=0)
    is_active = models.BooleanField(default=True)
    version_no = models.IntegerField(default=1)

    class Meta:
        db_table = "master_value"
        unique_together = [("table", "code")]

    @classmethod
    def values(cls, table):
        return list(cls.objects.filter(table=table, is_active=True)
                    .order_by("sort_order", "code"))


class SectorTaxonomyMap(models.Model):
    """Founder sector → canonical sector + adjacency (GC-01)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    founder_sector = models.CharField(max_length=128, unique=True)
    canonical_sector = models.CharField(max_length=128)
    canonical_subsector = models.CharField(max_length=128, blank=True, default="")
    adjacent_sectors = models.JSONField(default=list, blank=True)
    version_no = models.IntegerField(default=1)

    class Meta:
        db_table = "sector_taxonomy_map"


class InvestorCategoryMap(models.Model):
    """round × raise-band → {primary[], secondary[], optional[]} (GC-03)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    round_type = models.CharField(max_length=32)
    raise_band = models.CharField(max_length=32)     # e.g. "usd_0_1m"
    band_low_usd = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    band_high_usd = models.DecimalField(max_digits=20, decimal_places=2, default=0)
    primary = models.JSONField(default=list)
    secondary = models.JSONField(default=list)
    optional = models.JSONField(default=list)
    version_no = models.IntegerField(default=1)

    class Meta:
        db_table = "m_investor_category_map"
        unique_together = [("round_type", "raise_band")]
