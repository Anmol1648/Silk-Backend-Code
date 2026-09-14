"""
LLM platform models (Doc 2 M0.5 / Doc 6 §3) — ported from Gyain
`dbal/models_usage.py` LLMEndpoint / LLMModelCost / LLMCallLog, plus the
FundOS `LLMRoleBinding` addition.

Secrets stay out of the DB: the env-var NAME is stored, never the key.
"""
import logging
import os
import uuid

from django.conf import settings
from django.core.exceptions import ValidationError
from django.db import models

# The search cap applied when a config profile leaves max_uses blank.
#
# Six is the number the Anthropic advanced tier has always used, so this
# changes no existing behaviour where a cap was set — it only stops a blank
# from meaning "send the tool and say nothing about it".
DEFAULT_SEARCH_MAX_USES = 6

logger = logging.getLogger(__name__)

# Doc 5 Annexure C role list (Doc 2 M0.5 llm_role_binding).
LLM_ROLES = (
    "research_synthesis", "material_critique", "readiness_summary",
    "peer_insight", "raise_narrative", "valuation_negotiation",
    "valuation_explainer",                       # was missing from choices
    "instrument_explainer", "strategy_blueprint", "investment_story",
    "teaser", "pitch_story", "pitch_slides", "fin_model_assist",
    "fin_review", "im_section", "im_narrative", "im_consistency",
    "package_review", "objection_sim",
    # Company Profile generation roles — bindable and mock-toggleable in the
    # admin like any other role.
    "company_profile_section", "company_profile_records",
    "company_profile_structured",                # called in code; was missing
    "company_profile_field", "founder_profile",
    # NEW — consolidated deep extract (advanced) + grounded Q&A (simple).
    "company_profile_deep_extract", "profile_qa",
    # Company Master Data Pipeline. Two roles, one per half of a split that
    # used to be one call: `profile_research_batch` retrieves (web search on,
    # markdown out, ten calls per run) and `profile_synthesis` structures
    # (search off, JSON out, one call over the assembled dossier). Splitting
    # them is what lets the structuring half have a response schema, which no
    # search-capable role could previously keep.
    "profile_research_batch", "profile_synthesis",
    # Roles that were CALLED in code but never declared here. LLM_ROLES is the
    # choice list for LLMRoleBinding, so an undeclared role cannot be bound in
    # the admin console — and llm_generate raises LLMUnavailable when a role
    # has no binding. Each of these therefore failed at runtime on any system
    # with mocked AI switched off, with no way for an administrator to fix it.
    "company_profile_judgment",     # adjudicates conflicting extracted figures
    "assessment_extraction",        # fundos/assessment/extraction.py
    "assessment_rubric_review",     # fundos/assessment/tasks.py
    # Grounded Q&A over one deal's SCORECARD, and the overrides it may
    # propose. Declared here so an administrator can bind and mock it like
    # any other role -- an undeclared role has no binding, and llm_generate
    # raises rather than falling back.
    "assessment_qa",                # fundos/assessment/qa.py
    # v23 Phase 3 — emits the assessment workbook's own input_key values from
    # step 1 research, so the company profile finally reaches the scorecard.
    "assessment_inputs",            # fundos/profile/assessment_extraction.py
    "investor_classification",      # fundos/investors/classify.py
    "investor_identity",            # fundos/investors/identity.py
    # Reads an uploaded document — PDF, deck or image — by sending the FILE
    # to a model that accepts documents natively, replacing the local OCR
    # engines. Must be bound to a Gemini endpoint: `_dispatch` refuses
    # attachments on any provider that cannot read them, rather than
    # answering from the prompt alone and returning a summary of nothing.
    "document_read",                # fundos/profile/pipeline/document_ai.py
)

# Two operating tiers a role can run on. The concrete model + capabilities for
# each tier are chosen per tenant (see TenantLLMTier); "advanced" is the
# web-search tier, "simple" is the cheap no-tools tier.
# ---------------------------------------------------------------------------
# TIERS — the three KINDS of LLM work FundOS does.
#
# A tier answers "what kind of call is this?", never "which model?". The model
# is chosen per tenant in TenantLLMTier, so switching vendor or model is
# admin work, not a deploy. Nothing here is provider-specific.
#
# The stored values are stable machine keys. The labels below are what the
# admin sees; change a label freely, never a key (tenant rows point at keys).
# ---------------------------------------------------------------------------
SIMPLE = "simple"
ADVANCED = "advanced"
JUDGMENT = "judgment"

LLM_TIERS = (
    (SIMPLE, "Simple — extract from supplied text (no tools)"),
    (ADVANCED, "Advanced / Web — retrieve from the internet (web search on)"),
    (JUDGMENT, "Judgement — reason over already-extracted data (no tools)"),
)

# Surfaced in the admin so a configurator can pick a tier without reading
# code. Keep this the single source of truth for what a tier MEANS.
TIER_DEFINITIONS = {
    SIMPLE: {
        "label": "Simple",
        "purpose": "Pull structured values out of text the system already "
                   "holds — website extract, uploaded documents, founder "
                   "input.",
        "tools": "None. Cannot browse.",
        "use_when": "The answer is present in the supplied context and the "
                    "task is finding and shaping it.",
        "cost": "Lowest. Highest call volume — most calls in the system are "
                "this tier, so the model chosen here dominates the bill.",
        "typical_model": "A small/fast model.",
    },
    ADVANCED: {
        "label": "Advanced / Web",
        "purpose": "Discover facts that are NOT in the supplied context by "
                   "searching the public internet, then structure them.",
        "tools": "Web search (and URL fetch where the provider supports it).",
        "use_when": "The task needs information nobody uploaded — filings, "
                    "news, competitor data, regulatory changes.",
        "cost": "Highest per call. Each search round trip re-bills the "
                "accumulated context, so bound it with max_uses.",
        "typical_model": "A mid-tier model. Quality here is driven far more "
                         "by SEARCH COVERAGE (max_uses, query quality) than "
                         "by model strength — test max_uses before paying "
                         "for a bigger model.",
    },
    JUDGMENT: {
        "label": "Judgement",
        "purpose": "Reason over data another call already extracted: "
                   "adjudicate conflicting figures, flag outliers, assess "
                   "risk, write a thesis anchored in the data.",
        "tools": "None. Deliberately cannot browse — it must reason from "
                 "the extraction, not go looking for new facts.",
        "use_when": "The inputs are already structured and the value comes "
                    "from interpretation rather than retrieval.",
        "cost": "High per token, but the INPUT is small (the extracted JSON, "
                "not the source corpus), so a frontier model is affordable "
                "here even when it is not affordable for retrieval.",
        "typical_model": "The strongest model you are willing to pay for. "
                         "This is the one tier where model strength, not "
                         "tooling, is the binding constraint.",
    },
}


def tier_help_text():
    """Human-readable tier guide, rendered into admin help text."""
    parts = []
    for key, d in TIER_DEFINITIONS.items():
        parts.append(
            f"{d['label']} ({key}): {d['purpose']} Tools: {d['tools']} "
            f"Use when: {d['use_when']}")
    return "  |  ".join(parts)


class LLMEndpoint(models.Model):
    """Admin-editable endpoint registry (Doc 6 §3.1)."""

    PROVIDER_KINDS = (
        ("openai_chat", "OpenAI Chat"),
        ("anthropic", "Anthropic"),
        ("gemini", "Google Gemini"),
        ("deepinfra", "DeepInfra (OpenAI-compatible)"),
        ("custom_rest", "Custom REST"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=32, unique=True, db_index=True,
        help_text="Stable id (OPENAI/ANTHROPIC/GEMINI/LOCALGPU). Never change "
                  "once in use — cost rows reference it.")
    display_name = models.CharField(max_length=128, blank=True, default="")
    provider_kind = models.CharField(max_length=32, choices=PROVIDER_KINDS)
    base_url = models.CharField(
        max_length=512, blank=True, default="",
        help_text="Inference server base, no trailing slash.")
    default_model = models.CharField(max_length=128, blank=True, default="")
    path_template = models.CharField(
        max_length=255, blank=True, default="",
        help_text="For custom_rest: e.g. /generate/{model}/")
    api_key_env_var = models.CharField(
        max_length=128, blank=True, default="",
        help_text="NAME of the env var holding the key — value stays in secrets.")
    timeout_seconds = models.IntegerField(default=60)
    is_active = models.BooleanField(default=True)
    priority = models.IntegerField(default=100, help_text="Lower = preferred.")
    notes = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "llm_endpoint"

    def __str__(self):
        return f"{self.code} ({self.provider_kind})"

    # --- model helpers (Doc 6 §3.1, implement exactly) ---
    @property
    def api_key(self):
        """One usable key, rotated across the pool the env var provides.

        The variable may hold a single key (the original contract, unchanged)
        or a separated list. A profile run dispatches ten grounded calls, up to
        five concurrently, and a provider's per-minute quota is per key — so
        one key throttles a run that several would complete. See
        :mod:`fundos.llm.keyring`.
        """
        if not self.api_key_env_var:
            return None
        from fundos.llm import keyring
        return keyring.next_key(self.api_key_env_var)

    @property
    def api_key_pool_size(self) -> int:
        """How many keys are configured for this endpoint. Diagnostics only."""
        from fundos.llm import keyring
        return keyring.pool_size(self.api_key_env_var)

    def penalise_api_key(self, key, *, reason=""):
        """Cool a key that reported quota exhaustion, so the next call skips it."""
        from fundos.llm import keyring
        keyring.penalise(self.api_key_env_var, key, reason=reason)

    def disable_api_key(self, key, *, reason=""):
        """Drop a key the provider rejected as invalid, so no call uses it."""
        from fundos.llm import keyring
        keyring.disable(self.api_key_env_var, key, reason=reason)

    def has_api_key_in_env(self) -> bool:
        return bool(self.api_key)

    def build_url(self, model_override=None) -> str:
        model = model_override or self.default_model
        path = (self.path_template or "").format(model=model)
        return f"{self.base_url.rstrip('/')}{path}"


# Only these kinds require an API key (custom_rest may run keyless).
KINDS_REQUIRING_KEY = {"openai_chat", "anthropic", "gemini", "deepinfra"}


class LLMModelCost(models.Model):
    """Admin-editable price book (Doc 2 M0.5). model_name='*' = wildcard.

    Cache-aware: reads and writes of a cached prefix bill at different rates
    from fresh input (typically 0.1x and 1.25x respectively). When the cache
    columns are left at zero they fall back to multiples of the input rate,
    so an existing price book keeps producing sane numbers without editing.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=32, db_index=True)
    model_name = models.CharField(max_length=128, db_index=True)
    input_cost_per_1k_inr = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    output_cost_per_1k_inr = models.DecimalField(max_digits=12, decimal_places=6, default=0)
    cache_read_cost_per_1k_inr = models.DecimalField(
        max_digits=12, decimal_places=6, default=0,
        help_text="Blank/0 → derived as 0.1x the input rate.")
    cache_write_cost_per_1k_inr = models.DecimalField(
        max_digits=12, decimal_places=6, default=0,
        help_text="Blank/0 → derived as 1.25x the input rate (5-minute TTL).")
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "llm_model_cost"
        unique_together = [("provider", "model_name")]

    # Provider default multipliers, applied when a rate is not set explicitly.
    CACHE_READ_MULTIPLIER = "0.10"
    CACHE_WRITE_MULTIPLIER = "1.25"

    @classmethod
    def cost_inr(cls, provider, model_name, prompt_tokens, completion_tokens,
                 *, cache_read_tokens=0, cache_write_tokens=0):
        from decimal import Decimal
        row = (cls.objects.filter(provider=provider, model_name=model_name,
                                  is_active=True).first()
               or cls.objects.filter(provider=provider, model_name="*",
                                     is_active=True).first())
        if not row:
            # A missing price row silently reported ZERO spend, which is how a
            # cost dashboard ends up lying. Make it loud instead.
            logger.warning(
                "LLM: no active LLMModelCost row for provider=%s model=%s — "
                "this call is being recorded at zero cost. Seed the price "
                "book (manage.py seed_llm_costs) or spend reporting is wrong.",
                provider, model_name)
            return Decimal("0")

        read_rate = row.cache_read_cost_per_1k_inr or (
            row.input_cost_per_1k_inr * Decimal(cls.CACHE_READ_MULTIPLIER))
        write_rate = row.cache_write_cost_per_1k_inr or (
            row.input_cost_per_1k_inr * Decimal(cls.CACHE_WRITE_MULTIPLIER))

        return (Decimal(prompt_tokens or 0) / 1000 * row.input_cost_per_1k_inr
                + Decimal(completion_tokens or 0) / 1000 * row.output_cost_per_1k_inr
                + Decimal(cache_read_tokens or 0) / 1000 * read_rate
                + Decimal(cache_write_tokens or 0) / 1000 * write_rate)


class LLMCallLog(models.Model):
    """One row per LLM call — written by the adapter (Doc 6 §3.2/D).
    Monthly-partitioned in PG; drives per-deal AI-spend dashboards."""

    id = models.BigAutoField(primary_key=True)
    created_at = models.DateTimeField(auto_now_add=True, db_index=True)
    tenant_id = models.UUIDField(null=True, blank=True, db_index=True)
    provider = models.CharField(max_length=32, blank=True, default="")   # endpoint code
    model_name = models.CharField(max_length=128, blank=True, default="")
    function_name = models.CharField(max_length=64, blank=True, default="",
                                     db_index=True)  # role
    calling_context = models.CharField(max_length=128, blank=True, default="")
    deal_id = models.UUIDField(null=True, blank=True, db_index=True)
    req_user = models.ForeignKey(settings.AUTH_USER_MODEL, null=True, blank=True,
                                 on_delete=models.SET_NULL, related_name="+")
    is_user_initiated = models.BooleanField(default=True)
    prompt_tokens = models.IntegerField(default=0)
    completion_tokens = models.IntegerField(default=0)
    # Cached-prefix accounting. Reads bill at ~0.1x input and writes at ~1.25x,
    # and neither is included in prompt_tokens by the provider.
    cache_read_tokens = models.IntegerField(default=0)
    cache_write_tokens = models.IntegerField(default=0)
    # Web searches actually performed inside this call. -1 = the provider
    # gave us nothing to count from, which must not be confused with a
    # genuine zero. Only Anthropic ENFORCES max_uses; on Gemini and OpenAI
    # this column is the sole evidence of whether the cap was respected.
    search_count = models.IntegerField(
        default=-1,
        help_text="Searches performed in this call. -1 = unknown/not "
                  "reported by the provider.")
    total_tokens = models.IntegerField(default=0)
    response_time_ms = models.IntegerField(default=0)
    success = models.BooleanField(default=True)
    status = models.CharField(max_length=16, blank=True, default="success",
                              help_text="success | fallback | error | mocked")
    error_message = models.TextField(blank=True, default="")
    cost_inr = models.DecimalField(max_digits=14, decimal_places=6, default=0)

    # ---- Diagnostics: what it takes to IMPROVE an algorithm, not just bill
    # for it. Cost and latency tell you a call was expensive; these tell you
    # whether it was any good and what to change.
    tier = models.CharField(
        max_length=16, blank=True, default="", db_index=True,
        help_text="simple / advanced / judgment as resolved for this call.")
    config_profile = models.CharField(
        max_length=64, blank=True, default="", db_index=True,
        help_text="Which LLMConfigProfile served the call. Without this you "
                  "cannot attribute a quality change to a model change.")
    prompt_fingerprint = models.CharField(
        max_length=16, blank=True, default="", db_index=True,
        help_text="Short hash of the system prompt actually sent. Changes "
                  "when an admin edits the prompt, so results can be "
                  "compared BEFORE and AFTER a prompt edit.")
    context_chars = models.IntegerField(
        default=0,
        help_text="Size of the context block sent. Correlates input volume "
                  "with output quality — the key input to context tuning.")
    was_repaired = models.BooleanField(
        default=False,
        help_text="A second call was needed because JSON did not parse. A "
                  "cheap model with a high repair rate is not cheap.")
    was_normalized = models.BooleanField(
        default=False,
        help_text="The response came back under unexpected keys and had to "
                  "be remapped. Rising rates signal prompt/model drift.")
    output_signals = models.JSONField(
        default=dict, blank=True,
        help_text="Quality signals parsed from the response: needsInput, "
                  "counts of missing/conflicts/records. These, not cost, are "
                  "what tell you whether a prompt or model change helped.")

    class Meta:
        db_table = "llm_call_log"


class LLMRoleBinding(models.Model):
    """Per-role primary + fallback endpoint selection (Doc 6 §3.3)."""

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    role = models.CharField(max_length=48, unique=True,
                            choices=[(r, r) for r in LLM_ROLES])
    primary_endpoint = models.ForeignKey(
        LLMEndpoint, on_delete=models.PROTECT, related_name="primary_roles",
        to_field="code", db_column="endpoint_code")
    fallback_endpoint = models.ForeignKey(
        LLMEndpoint, null=True, blank=True, on_delete=models.SET_NULL,
        related_name="fallback_roles", to_field="code", db_column="fallback_code")
    is_mocked = models.BooleanField(default=False)
    # Blank = "use the budget this role was designed for"
    # (llm.adapter.ROLE_MAX_OUTPUT_TOKENS, 2048 for roles with no entry).
    #
    # This was a non-null field defaulting to 2048, which made "the shipped
    # default" indistinguishable from "an administrator chose 2048". The
    # adapter can only ever LOWER the value, so every fresh install ran the
    # dossier role at 2048 of its designed 8192 and returned a truncated
    # dossier that logged as a successful call.
    max_output_tokens = models.IntegerField(
        null=True, blank=True, default=None,
        help_text="Leave EMPTY to use the role's designed output budget "
                  "(recommended). Set a number only to cap this role BELOW "
                  "that budget — the value never raises it.")
    temperature = models.FloatField(null=True, blank=True,
                                    help_text="Deterministic paths pass 0; "
                                              "leave empty for provider default.")

    class Meta:
        db_table = "llm_role_binding"

    def __str__(self):
        return f"{self.role} → {self.primary_endpoint_id}"


# ---------------------------------------------------------------------------
# Configurable multi-LLM capability layer (Feature Requirement Doc §4)
# ---------------------------------------------------------------------------
# Canonical provider identifiers used by the capability layer. These map onto
# the LLMEndpoint.provider_kind values but are the stable keys the config
# profiles and model catalog are grouped by.
CONFIG_PROVIDERS = (
    ("anthropic", "Anthropic (Claude)"),
    ("gemini", "Google (Gemini)"),
    ("openai", "OpenAI"),
)

# Map an endpoint's provider_kind onto a config provider key.
ENDPOINT_KIND_TO_PROVIDER = {
    "anthropic": "anthropic",
    "gemini": "gemini",
    "openai_chat": "openai",
    "deepinfra": "openai",       # OpenAI-compatible
}

# Which capability toggles each provider actually supports. The admin layer
# uses this to show only valid settings and to silently ignore the rest; the
# adapter uses it to decide what to send. (Validate against live provider docs
# at implementation time — these evolve; §4.2.)
PROVIDER_CAPABILITIES = {
    "anthropic": {
        "web_search", "web_fetch", "domain_filter", "max_uses",
        "user_location", "dynamic_filter", "function_calling",
        "code_execution", "structured_output", "thinking_budget",
        "prompt_cache",        # explicit cache_control breakpoints
    },
    "gemini": {
        "web_search",          # Google Search grounding
        "url_context",         # fetch/read provided URLs
        "maps_grounding",
        "function_calling",    # NOTE: not combinable with web_search
        "code_execution",
        "structured_output",
        "thinking_budget",     # thinkingConfig.thinkingBudget
        "prompt_cache",        # implicit context caching (no wire change)
    },
    "openai": {
        "web_search", "function_calling", "code_execution",
        "structured_output", "reasoning_effort",
        "prompt_cache",        # automatic prompt caching (no wire change)
    },
}

# How each provider realises prompt caching. The adapter uses this to decide
# whether to emit explicit cache breakpoints or simply keep the prefix stable
# and let the provider cache it automatically. Either way the SAVING is real
# and the cached-token counts are read back uniformly, so the admin sees one
# consistent number regardless of which LLM a tenant runs on.
#   "explicit" — caller marks breakpoints (Anthropic cache_control)
#   "implicit" — provider caches a repeated prefix on its own; the caller's
#                job is only to keep that prefix byte-stable and first
CACHE_STRATEGY = {
    "anthropic": "explicit",
    "gemini": "implicit",
    "openai": "implicit",
}


class LLMModelCatalog(models.Model):
    """Editable list of available models per provider (§4.4).

    New model releases are added here (no code deploy); retired models are
    HIDDEN (is_active=False) rather than deleted, so historical config
    profiles that reference them keep working.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    provider = models.CharField(max_length=16, choices=CONFIG_PROVIDERS,
                                db_index=True)
    model_string = models.CharField(
        max_length=128,
        help_text="Exact API model string, e.g. gemini-2.5-flash, "
                  "claude-sonnet-4-6, gpt-4o.")
    display_name = models.CharField(max_length=128, blank=True, default="")
    is_active = models.BooleanField(
        default=True,
        help_text="Untick to retire/hide a model from selection without "
                  "deleting configs that reference it.")
    notes = models.TextField(blank=True, default="")
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "llm_model_catalog"
        unique_together = [("provider", "model_string")]
        ordering = ("provider", "model_string")

    def __str__(self):
        flag = "" if self.is_active else " (hidden)"
        return f"{self.provider}:{self.model_string}{flag}"


class LLMConfigProfile(models.Model):
    """A reusable per-call LLM configuration (§4.3).

    Carries WHAT the call is allowed to do (capabilities) and HOW it should
    respond (system instructions, structured-output schema, thinking budget).
    Connection details (base_url, api key, fallback, logging) are NOT
    duplicated here — the profile references an existing LLMEndpoint, so the
    established fallback/logging path stays intact.

    A feature (e.g. one Company Profile extraction section) attaches a profile
    by its `code` rather than hard-coding provider logic.
    """

    THINKING_UNITS = (
        ("none", "Off"),
        ("tokens", "Token budget (Anthropic extended thinking)"),
        ("effort", "Effort level (OpenAI reasoning)"),
    )

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    code = models.CharField(
        max_length=64, unique=True, db_index=True,
        help_text="Stable id a feature references, e.g. "
                  "cp_extract.basic_info or cp_structure.claude.")
    display_name = models.CharField(max_length=128, blank=True, default="")
    provider = models.CharField(max_length=16, choices=CONFIG_PROVIDERS)
    model_string = models.CharField(
        max_length=128,
        help_text="Chosen from the model catalog for this provider.")
    # Connection reuse — keeps fallback + logging intact.
    endpoint = models.ForeignKey(
        LLMEndpoint, on_delete=models.PROTECT, related_name="config_profiles",
        to_field="code", db_column="endpoint_code",
        help_text="Connection (base_url + API key) this profile runs on.")

    # Which KIND of work this profile serves. See TIER_DEFINITIONS for the
    # authoritative description of each; the help text below is generated
    # from it so admin and code can never drift apart.
    tier = models.CharField(
        max_length=16, choices=LLM_TIERS, default=SIMPLE, db_index=True,
        help_text="Which kind of work this model serves. " + tier_help_text())

    system_instructions = models.TextField(blank=True, default="")

    # Cost control — cache the stable prompt prefix (role instructions + the
    # run's source bundle) so repeated section calls read it at ~0.1x input
    # instead of paying full price each time. Anthropic only today; harmless
    # elsewhere. Off only if a provider/model in use lacks cache support.
    enable_prompt_cache = models.BooleanField(
        default=True,
        help_text="Cache the stable prompt prefix across calls. Leave ON "
                  "unless the model does not support prompt caching.")

    # --- Tool / capability toggles (only those valid for the provider take
    #     effect; the rest are ignored by the adapter). ---
    web_search = models.BooleanField(
        default=False,
        help_text="Live web search (Claude Web Search / Gemini Search "
                  "grounding / OpenAI Web Search).")
    web_fetch = models.BooleanField(
        default=False,
        help_text="Fetch a specific URL (Claude Web Fetch / Gemini URL "
                  "context).")
    maps_grounding = models.BooleanField(
        default=False, help_text="Gemini only — Maps grounding.")
    function_calling = models.BooleanField(default=False)
    code_execution = models.BooleanField(default=False)

    # --- Web-search sub-controls (apply where the provider supports them) ---
    max_uses = models.IntegerField(
        null=True, blank=True,
        help_text="Cap searches per request. Blank uses the deployment "
                  "default (%d) — it never means 'unlimited' and never "
                  "means 'no directive'." % DEFAULT_SEARCH_MAX_USES)
    allowed_domains = models.JSONField(
        default=list, blank=True,
        help_text="Claude — restrict search to these domains. Mutually "
                  "exclusive with blocked_domains.")
    blocked_domains = models.JSONField(
        default=list, blank=True,
        help_text="Claude — exclude these domains. Mutually exclusive with "
                  "allowed_domains.")
    user_location = models.JSONField(
        default=dict, blank=True,
        help_text="Claude — localize search results (e.g. {'country':'IN'}).")
    dynamic_filter = models.BooleanField(
        default=False,
        help_text="Claude — code-filter search results before context "
                  "(requires code execution).")

    # --- Structured output ---
    structured_output = models.BooleanField(default=False)
    output_schema = models.JSONField(
        default=dict, blank=True,
        help_text="JSON schema the response must conform to when structured "
                  "output is on.")

    # --- Thinking / reasoning ---
    thinking_mode = models.CharField(max_length=8, choices=THINKING_UNITS,
                                     default="none")
    thinking_budget = models.CharField(
        max_length=32, blank=True, default="",
        help_text="Token count (Anthropic) or effort level low|medium|high "
                  "(OpenAI).")

    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "llm_config_profile"
        ordering = ("code",)

    def __str__(self):
        return f"{self.code} ({self.provider}:{self.model_string})"

    # --- validation: enforce the provider capability matrix + constraints ---
    def clean(self):
        caps = PROVIDER_CAPABILITIES.get(self.provider, set())

        # Endpoint provider must match this profile's provider.
        if self.endpoint_id:
            ep_provider = ENDPOINT_KIND_TO_PROVIDER.get(
                self.endpoint.provider_kind)
            if ep_provider and ep_provider != self.provider:
                raise ValidationError(
                    {"endpoint": f"Endpoint is a {ep_provider} connection but "
                                 f"this profile is {self.provider}."})

        # Gemini hard constraint: search tools cannot be combined with
        # function calling in the same request.
        if self.provider == "gemini" and self.function_calling and (
                self.web_search or self.maps_grounding):
            raise ValidationError(
                "Gemini cannot combine search/grounding tools with function "
                "calling in one request. Turn off one of them.")

        # Claude: dynamic result filtering requires code execution.
        if self.dynamic_filter and not self.code_execution:
            raise ValidationError(
                {"dynamic_filter": "Dynamic filtering requires code execution "
                                   "to be enabled."})

        # Domain allow/block are mutually exclusive.
        if self.allowed_domains and self.blocked_domains:
            raise ValidationError(
                "Provide allowed_domains OR blocked_domains, not both.")

        # Web-search sub-controls only make sense with web_search on.
        if (self.max_uses or self.allowed_domains or self.blocked_domains
                or self.user_location or self.dynamic_filter) and \
                not self.web_search:
            raise ValidationError(
                "Web-search sub-controls require web_search to be enabled.")

        # Thinking budget consistency.
        if self.thinking_mode == "effort" and self.thinking_budget and \
                self.thinking_budget.lower() not in ("low", "medium", "high"):
            raise ValidationError(
                {"thinking_budget": "Effort must be low, medium, or high."})

        # Warn-by-rejecting capabilities the provider does not support at all,
        # so an admin can't silently rely on an unsupported toggle.
        requested = set()
        if self.web_search:
            requested.add("web_search")
        if self.web_fetch:
            requested.add("web_fetch" if "web_fetch" in caps else "url_context")
        if self.maps_grounding:
            requested.add("maps_grounding")
        if self.function_calling:
            requested.add("function_calling")
        if self.code_execution:
            requested.add("code_execution")
        if self.structured_output:
            requested.add("structured_output")
        unsupported = requested - caps
        if unsupported:
            raise ValidationError(
                f"{self.provider} does not support: {', '.join(sorted(unsupported))}.")

        # v27.3 — the tier is a CONTRACT, not a label. A profile that cannot
        # honour its tier is rejected at save time rather than discovered by
        # diagnose_config after a run has already published ungrounded
        # output. See fundos/llm/tier_contract.py for why.
        from fundos.llm.tier_contract import (TierContractViolation,
                                              validate_profile_against_tier)
        try:
            validate_profile_against_tier(self, self.tier, caps)
        except TierContractViolation as e:
            raise ValidationError({"tier": str(e)})

    def capabilities_payload(self) -> dict:
        """The normalized capability set the adapter will translate to
        provider-specific API params. Only provider-supported toggles are
        included; everything else is dropped here so downstream code is
        provider-agnostic."""
        caps = PROVIDER_CAPABILITIES.get(self.provider, set())
        out = {}
        if self.web_search and "web_search" in caps:
            ws = {}
            # ALWAYS carry a cap. Leaving max_uses blank used to produce
            # `web_search: {}` — an empty dict, which is truthy nowhere and
            # present everywhere. The adapter attaches the tool on key
            # PRESENCE but builds the search directive on TRUTHINESS, so a
            # blank cap sent Gemini the google_search tool with no
            # instruction to use it at all. google_search is model-decided:
            # granted permission and told nothing, the model answered from
            # context every time, on every call, in every run — with a 200
            # and valid JSON, so nothing anywhere reported a fault.
            #
            # Blank now means "the deployment default", not "no opinion".
            ws["max_uses"] = self.max_uses or DEFAULT_SEARCH_MAX_USES
            if self.allowed_domains:
                ws["allowed_domains"] = self.allowed_domains
            if self.blocked_domains:
                ws["blocked_domains"] = self.blocked_domains
            if self.user_location:
                ws["user_location"] = self.user_location
            if self.dynamic_filter and "dynamic_filter" in caps:
                ws["dynamic_filter"] = True
            out["web_search"] = ws
        if self.web_fetch and ({"web_fetch", "url_context"} & caps):
            out["url_fetch"] = True
        if self.maps_grounding and "maps_grounding" in caps:
            out["maps_grounding"] = True
        if self.function_calling and "function_calling" in caps:
            out["function_calling"] = True
        if self.code_execution and "code_execution" in caps:
            out["code_execution"] = True
        if self.structured_output and "structured_output" in caps:
            out["structured_output"] = {"schema": self.output_schema or {}}
        if self.thinking_mode != "none":
            if self.thinking_mode == "tokens" and "thinking_budget" in caps:
                out["thinking"] = {"budget_tokens": self.thinking_budget}
            elif self.thinking_mode == "effort" and "reasoning_effort" in caps:
                out["reasoning_effort"] = self.thinking_budget or "medium"

        # v27.3 — THE TIER HAS THE LAST WORD.
        #
        # Everything above assembles what the PROFILE asked for. The contract
        # below enforces what the TIER means, because the two used to be able
        # to disagree: `tier="advanced", web_search=False` was a valid row
        # that saved without complaint and generated a profile from the
        # model's memory while every layer reported healthy.
        #
        # This also subsumes the old `elif provider == "gemini"` branch that
        # forced budget_tokens=0 whenever thinking_mode was unstated. That
        # default was correct for extraction and catastrophic for retrieval:
        # google_search is MODEL-DECIDED, so a search tool handed to a model
        # with no deliberation budget is permission granted to something with
        # no opportunity to take it. The contract decides per tier instead of
        # per provider.
        from fundos.llm.tier_contract import apply_contract
        return apply_contract(out, self.tier, provider=self.provider,
                              provider_caps=caps, profile=self)


# ---------------------------------------------------------------------------
# Tenant-level LLM tiers (Simple / Advanced) — the two LLMs configured per
# tenant. A prompt's tier picks which of these two a call uses.
# ---------------------------------------------------------------------------
class TenantLLMTier(models.Model):
    """Binds a tenant's Simple / Advanced slot to an LLMConfigProfile.

    A row with tenant_id = NULL is the GLOBAL default used by any tenant that
    has not configured its own tier — so a fresh tenant works out of the box.
    A tenant-specific row overrides the global default for that tenant only.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="NULL = global default for all tenants.")
    tier = models.CharField(max_length=16, choices=LLM_TIERS)
    config_profile = models.ForeignKey(
        LLMConfigProfile, on_delete=models.PROTECT, related_name="tenant_tiers",
        to_field="code", db_column="config_profile_code",
        help_text="The model + capabilities used for this tenant/tier.")
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenant_llm_tier"
        constraints = [
            models.UniqueConstraint(
                fields=["tenant_id", "tier"], name="uq_tenant_llm_tier"),
            # PostgreSQL treats NULLs as DISTINCT, so the constraint above
            # never protected the GLOBAL rows (tenant_id IS NULL) — the seeder
            # could and did create duplicates, after which get_or_create threw
            # MultipleObjectsReturned and aborted the whole seed run.
            models.UniqueConstraint(
                fields=["tier"], condition=models.Q(tenant_id__isnull=True),
                name="uq_tenant_llm_tier_global"),
        ]

    def __str__(self):
        who = self.tenant_id or "GLOBAL"
        return f"{who} · {self.tier} → {self.config_profile_id}"

    @classmethod
    def resolve_code(cls, tier, tenant_id=None):
        """Config-profile CODE for (tenant, tier); tenant row wins, else the
        global (tenant_id NULL) default, else None."""
        if tenant_id is not None:
            row = cls.objects.filter(
                tenant_id=tenant_id, tier=tier, is_active=True
            ).values_list("config_profile_id", flat=True).first()
            if row:
                return row
        return cls.objects.filter(
            tenant_id__isnull=True, tier=tier, is_active=True
        ).values_list("config_profile_id", flat=True).first()


class TenantLLMBudget(models.Model):
    """Monthly AI spend ceiling per tenant (cost control).

    Runaway loops — a retry storm, a bulk import that regenerates every
    profile — are how an LLM bill actually blows up, not one expensive call.
    The adapter consults this BEFORE dispatch and refuses to spend past the
    cap, so the failure is a clean E-LLM-502 instead of an invoice.

    Absence of a row means NO CAP, so this is opt-in and cannot break an
    existing deployment. A NULL tenant_id row acts as the global default.
    """

    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)
    tenant_id = models.UUIDField(
        null=True, blank=True, db_index=True,
        help_text="NULL = global default ceiling for tenants without a row.")
    monthly_cap_inr = models.DecimalField(
        max_digits=14, decimal_places=2, default=0,
        help_text="0 = unlimited. Otherwise calls are refused once "
                  "month-to-date spend reaches this figure.")
    alert_at_pct = models.IntegerField(
        default=80,
        help_text="Log a warning once month-to-date spend crosses this "
                  "percentage of the cap.")
    is_active = models.BooleanField(default=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        db_table = "tenant_llm_budget"
        constraints = [
            models.UniqueConstraint(fields=["tenant_id"],
                                    name="uq_tenant_llm_budget"),
            # Same NULL-distinctness problem as TenantLLMTier: the global
            # budget row (tenant_id IS NULL) was unprotected and could be
            # duplicated, which then breaks every read that expects one row.
            models.UniqueConstraint(
                models.functions.Coalesce(
                    "tenant_id",
                    models.Value("00000000-0000-0000-0000-000000000000",
                                 output_field=models.UUIDField())),
                name="uq_tenant_llm_budget_any"),
        ]

    def __str__(self):
        who = self.tenant_id or "GLOBAL"
        return f"{who} · ₹{self.monthly_cap_inr}/month"

    @classmethod
    def resolve(cls, tenant_id=None):
        """The applicable budget row for a tenant, or None for no cap."""
        if tenant_id is not None:
            row = cls.objects.filter(tenant_id=tenant_id,
                                     is_active=True).first()
            if row:
                return row
        return cls.objects.filter(tenant_id__isnull=True,
                                  is_active=True).first()

    @classmethod
    def month_to_date_inr(cls, tenant_id=None):
        """Sum of logged spend for the current calendar month."""
        from decimal import Decimal

        from django.db.models import Sum
        from django.utils import timezone

        start = timezone.now().replace(day=1, hour=0, minute=0, second=0,
                                       microsecond=0)
        qs = LLMCallLog.objects.filter(created_at__gte=start)
        if tenant_id is not None:
            qs = qs.filter(tenant_id=tenant_id)
        return qs.aggregate(total=Sum("cost_inr"))["total"] or Decimal("0")

    @classmethod
    def check_budget(cls, tenant_id=None):
        """(allowed, spent_inr, cap_inr). allowed=True when under the cap.

        NB: deliberately not named `check` — that is a reserved Django
        Model classmethod used by the system-check framework, and shadowing
        it breaks `manage.py` entirely.
        """
        from decimal import Decimal

        row = cls.resolve(tenant_id)
        if row is None or not row.monthly_cap_inr:
            return True, Decimal("0"), Decimal("0")

        spent = cls.month_to_date_inr(tenant_id)
        cap = row.monthly_cap_inr
        if row.alert_at_pct and cap:
            threshold = cap * Decimal(row.alert_at_pct) / Decimal("100")
            if spent >= threshold and spent < cap:
                logger.warning(
                    "LLM BUDGET: tenant %s at ₹%s of ₹%s (%s%% alert "
                    "threshold crossed).", tenant_id or "GLOBAL", spent, cap,
                    row.alert_at_pct)
        return spent < cap, spent, cap
