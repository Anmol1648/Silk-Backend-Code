from django.contrib import admin

from fundos.llm.models import (
    LLMCallLog, LLMConfigProfile, LLMEndpoint, LLMModelCatalog, LLMModelCost,
    LLMRoleBinding, TenantLLMBudget, TenantLLMTier,
)


@admin.register(LLMEndpoint)
class LLMEndpointAdmin(admin.ModelAdmin):
    list_display = ("code", "provider_kind", "default_model", "is_active",
                    "priority")


@admin.register(LLMModelCatalog)
class LLMModelCatalogAdmin(admin.ModelAdmin):
    """Editable per-provider model list (§4.4). Add new models here without a
    code deploy; untick is_active to retire one without breaking configs."""
    list_display = ("provider", "model_string", "display_name", "is_active",
                    "updated_at")
    list_filter = ("provider", "is_active")
    list_editable = ("is_active",)
    search_fields = ("model_string", "display_name")


@admin.register(LLMConfigProfile)
class LLMConfigProfileAdmin(admin.ModelAdmin):
    """Admin switchboard for the per-call capability layer (§4.2/§4.3).

    The model's clean() enforces the provider capability matrix and the hard
    constraints (Gemini search-vs-function-calling; Claude dynamic-filter
    needs code-exec; domain allow/block exclusivity), so an invalid
    combination cannot be saved.
    """
    list_display = ("code", "tier", "provider", "model_string", "endpoint",
                    "web_search", "web_fetch", "structured_output",
                    "thinking_mode", "enable_prompt_cache", "is_active")
    list_filter = ("tier", "provider", "is_active", "web_search", "structured_output")
    search_fields = ("code", "display_name", "model_string")
    fieldsets = (
        (None, {
            "fields": ("code", "display_name", "tier", "provider", "model_string",
                       "endpoint", "system_instructions", "enable_prompt_cache",
                       "is_active"),
        }),
        ("Tools / capabilities", {
            "fields": ("web_search", "web_fetch", "maps_grounding",
                       "function_calling", "code_execution"),
            "description": "Only capabilities valid for the selected provider "
                           "take effect; unsupported toggles are rejected on "
                           "save. Gemini cannot combine search with function "
                           "calling.",
        }),
        ("Web-search sub-controls (Claude)", {
            "classes": ("collapse",),
            "fields": ("max_uses", "allowed_domains", "blocked_domains",
                       "user_location", "dynamic_filter"),
            "description": "Apply when web search is on. Dynamic filtering "
                           "requires code execution.",
        }),
        ("Structured output", {
            "fields": ("structured_output", "output_schema"),
        }),
        ("Thinking / reasoning", {
            "fields": ("thinking_mode", "thinking_budget"),
        }),
    )


@admin.register(LLMRoleBinding)
class LLMRoleBindingAdmin(admin.ModelAdmin):
    list_display = ("role", "primary_endpoint", "fallback_endpoint",
                    "is_mocked")
    list_editable = ("is_mocked",)


@admin.register(LLMCallLog)
class LLMCallLogAdmin(admin.ModelAdmin):
    """Per-call spend. `calling_context` is the lever for cost work — it
    identifies which feature (e.g. profile.generate.competitors) is running
    up the bill, so the worst offender can be pruned first."""
    list_display = ("function_name", "calling_context", "tier",
                    "config_profile", "model_name", "prompt_tokens",
                    "completion_tokens", "cache_read_tokens", "search_count",
                    "was_repaired", "was_normalized", "cost_inr", "status",
                    "created_at")
    list_filter = ("status", "tier", "config_profile", "function_name",
                   "model_name", "was_repaired", "was_normalized")
    search_fields = ("calling_context", "model_name", "prompt_fingerprint")
    readonly_fields = tuple(
        f.name for f in LLMCallLog._meta.fields if f.name != "id")


@admin.register(LLMModelCost)
class LLMModelCostAdmin(admin.ModelAdmin):
    """The price book. A MISSING row means calls are logged at zero cost and
    the spend dashboard silently lies — seed it with
    `manage.py seed_llm_costs`. Leave the cache columns at 0 to derive them
    as 0.1x / 1.25x of the input rate."""
    list_display = ("provider", "model_name", "input_cost_per_1k_inr",
                    "output_cost_per_1k_inr", "cache_read_cost_per_1k_inr",
                    "cache_write_cost_per_1k_inr", "is_active", "updated_at")
    list_filter = ("provider", "is_active")
    search_fields = ("provider", "model_name")


@admin.register(TenantLLMBudget)
class TenantLLMBudgetAdmin(admin.ModelAdmin):
    """Monthly spend ceiling, enforced BEFORE dispatch.

    No row = no cap. A row with tenant blank is the global default. This is
    the backstop against a runaway loop, which is how these bills actually
    blow up.
    """
    list_display = ("tenant_label", "monthly_cap_inr", "alert_at_pct",
                    "spent_this_month", "is_active", "updated_at")
    list_filter = ("is_active",)

    @admin.display(description="Tenant")
    def tenant_label(self, obj):
        return obj.tenant_id or "— GLOBAL default —"

    @admin.display(description="Spent (month to date)")
    def spent_this_month(self, obj):
        try:
            return f"Rs {TenantLLMBudget.month_to_date_inr(obj.tenant_id):.2f}"
        except Exception:
            return "—"


@admin.register(TenantLLMTier)
class TenantLLMTierAdmin(admin.ModelAdmin):
    """The two LLMs (Simple / Advanced) each tenant uses.

    A row with no tenant is the GLOBAL default for every tenant that has not
    configured its own. Point the 'advanced' slot at a web-search config and
    the 'simple' slot at a cheap no-tools config.
    """
    list_display = ("tenant_label", "tier", "config_profile", "is_active",
                    "updated_at")
    list_filter = ("tier", "is_active")
    search_fields = ("tenant_id", "config_profile__code")

    @admin.display(description="Tenant")
    def tenant_label(self, obj):
        return obj.tenant_id or "— GLOBAL default —"
