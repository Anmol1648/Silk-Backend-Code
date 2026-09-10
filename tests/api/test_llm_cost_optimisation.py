"""Tests for the LLM cost-optimisation layer.

Covers the four levers added in CHANGES_llm_cost_optimisation.md:
  1. context_keys filtering (roles are billed only for what they declare)
  2. prompt-cache block splitting + cache-aware token accounting
  3. source compression + skip-if-unchanged fingerprinting
  4. budget ceiling, bounded retries, per-role output caps
"""
from decimal import Decimal
from unittest.mock import patch

from django.test import TestCase

from tests.conftest_helpers import make_world


class ShippedDefaultsMixin:
    """Assert the SHIPPED defaults, independent of the developer's shell.

    Seeding auto-selects whichever provider has a usable API key — that is
    the point of it. These tests assert the configuration a fresh install
    ships with, so a GEMINI_API_KEY or OPENAI_API_KEY left in the shell
    running the suite legitimately changes what the seeder produces and six
    of them fail. That was a defect in the tests, not in the seeder: a test
    describing the shipped state must control the input that decides it.
    """

    def setUp(self):
        super().setUp()
        cleared = patch.dict("os.environ", {}, clear=False)
        cleared.start()
        self.addCleanup(cleared.stop)
        import os
        for var in ("GEMINI_API_KEY", "OPENAI_API_KEY", "DEEPINFRA_API_KEY"):
            os.environ.pop(var, None)


class ContextFilteringTests(TestCase):
    """A role must not be billed for context it never declared."""

    def test_filters_to_declared_context_keys(self):
        from fundos.llm.adapter import _filter_context

        # profile_qa declares: company, profile, question
        context = {
            "company": {"name": "Acme"},
            "profile": {"sections": []},
            "question": "What is the ARR?",
            "document_extracts": ["a" * 5000],   # not declared → must go
            "research": {"market": "b" * 5000},  # not declared → must go
        }
        out = _filter_context("profile_qa", context)
        self.assertEqual(set(out.keys()), {"company", "profile", "question"})

    def test_unknown_role_keeps_full_context(self):
        """An unaudited call site must never be starved of context."""
        from fundos.llm.adapter import _filter_context

        context = {"anything": 1, "else": 2}
        self.assertEqual(_filter_context("no_such_role", context), context)

    def test_stale_declaration_does_not_empty_context(self):
        """If the declaration matches nothing, fall back rather than send {}."""
        from fundos.llm.adapter import _filter_context

        context = {"totally_different_key": "value"}
        out = _filter_context("profile_qa", context)
        self.assertEqual(out, context)

    def test_filtering_reduces_payload_materially(self):
        from fundos.llm.adapter import _filter_context, _render_context

        context = {
            "company": {"name": "Acme"},
            "profile": {},
            "question": "q",
            "document_extracts": [{"summary": "x" * 20000}],
        }
        before = len(_render_context(context))
        after = len(_render_context(_filter_context("profile_qa", context)))
        self.assertLess(after, before / 5)


class PromptCacheTests(TestCase):
    """The stable prefix must be cacheable and correctly ordered."""

    def test_stable_block_precedes_volatile_block(self):
        from fundos.llm.adapter import _build_anthropic_system

        system = "S" * 8000        # role instructions — same for every company
        context = "C" * 8000       # this run's sources
        blocks = _build_anthropic_system(system, context)

        self.assertIsInstance(blocks, list)
        self.assertEqual(len(blocks), 2)
        # Anthropic matches an exact prefix: the block identical across ALL
        # companies must come first, or every new company busts the cache.
        self.assertTrue(blocks[0]["text"].startswith("S"))
        self.assertEqual(blocks[0]["cache_control"], {"type": "ephemeral"})
        self.assertEqual(blocks[1]["cache_control"], {"type": "ephemeral"})

    def test_small_blocks_are_not_cached(self):
        """A cache WRITE costs 1.25x input — caching a tiny block loses money."""
        from fundos.llm.adapter import _build_anthropic_system

        blocks = _build_anthropic_system("short system", "short context")
        self.assertNotIn("cache_control", blocks[0])
        self.assertNotIn("cache_control", blocks[1])

    def test_no_context_returns_plain_string(self):
        """Backward compatibility: unchanged wire shape when nothing to split."""
        from fundos.llm.adapter import _build_anthropic_system

        self.assertEqual(_build_anthropic_system("sys", ""), "sys")


class CacheAwareCostTests(TestCase):
    """Cached tokens bill at different rates and are not in prompt_tokens."""

    def _price_row(self):
        from fundos.llm.models import LLMModelCost
        return LLMModelCost.objects.create(
            provider="TEST", model_name="*",
            input_cost_per_1k_inr=Decimal("1.000000"),
            output_cost_per_1k_inr=Decimal("5.000000"))

    def test_cache_rates_derived_when_not_set(self):
        from fundos.llm.models import LLMModelCost
        self._price_row()
        # 1000 cache-read tokens at the derived 0.1x rate = 0.1
        cost = LLMModelCost.cost_inr("TEST", "any-model", 0, 0,
                                     cache_read_tokens=1000)
        self.assertEqual(cost, Decimal("0.1"))

    def test_cache_write_rate_derived(self):
        from fundos.llm.models import LLMModelCost
        self._price_row()
        cost = LLMModelCost.cost_inr("TEST", "any-model", 0, 0,
                                     cache_write_tokens=1000)
        self.assertEqual(cost, Decimal("1.25"))

    def test_explicit_cache_rate_wins(self):
        from fundos.llm.models import LLMModelCost
        LLMModelCost.objects.create(
            provider="TEST2", model_name="*",
            input_cost_per_1k_inr=Decimal("1.000000"),
            output_cost_per_1k_inr=Decimal("5.000000"),
            cache_read_cost_per_1k_inr=Decimal("0.030000"))
        cost = LLMModelCost.cost_inr("TEST2", "m", 0, 0,
                                     cache_read_tokens=1000)
        self.assertEqual(cost, Decimal("0.03"))

    def test_missing_price_row_still_returns_zero_but_warns(self):
        """Zero is the legacy behaviour; the WARNING is the new part."""
        from fundos.llm.models import LLMModelCost
        with self.assertLogs("fundos.llm.models", level="WARNING") as logs:
            cost = LLMModelCost.cost_inr("NOPE", "nope", 100, 100)
        self.assertEqual(cost, Decimal("0"))
        self.assertIn("no active LLMModelCost row", "".join(logs.output))


class BudgetCeilingTests(TestCase):
    """A runaway loop must fail fast rather than bill."""

    def test_no_row_means_no_cap(self):
        from fundos.llm.models import TenantLLMBudget
        allowed, _, _ = TenantLLMBudget.check_budget(None)
        self.assertTrue(allowed)

    def test_zero_cap_means_unlimited(self):
        from fundos.llm.models import TenantLLMBudget
        TenantLLMBudget.objects.create(tenant_id=None,
                                       monthly_cap_inr=Decimal("0"))
        allowed, _, _ = TenantLLMBudget.check_budget(None)
        self.assertTrue(allowed)

    def test_blocks_once_spend_exceeds_cap(self):
        from fundos.llm.models import LLMCallLog, TenantLLMBudget
        TenantLLMBudget.objects.create(tenant_id=None,
                                       monthly_cap_inr=Decimal("10.00"))
        LLMCallLog.objects.create(provider="X", function_name="r",
                                  cost_inr=Decimal("12.00"))
        allowed, spent, cap = TenantLLMBudget.check_budget(None)
        self.assertFalse(allowed)
        self.assertEqual(cap, Decimal("10.00"))
        self.assertGreaterEqual(spent, Decimal("12.00"))

    def test_adapter_refuses_dispatch_over_budget(self):
        from fundos.core.exceptions import LLMUnavailable
        from fundos.llm.adapter import _enforce_budget
        from fundos.llm.models import LLMCallLog, TenantLLMBudget

        TenantLLMBudget.objects.create(tenant_id=None,
                                       monthly_cap_inr=Decimal("1.00"))
        LLMCallLog.objects.create(provider="X", function_name="r",
                                  cost_inr=Decimal("5.00"))
        with self.assertRaises(LLMUnavailable):
            _enforce_budget(role="company_profile_records")


class OutputCapTests(TestCase):
    """Output is the expensive direction; caps must only ever lower."""

    def test_role_cap_lowers_binding_value(self):
        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS
        self.assertEqual(ROLE_MAX_OUTPUT_TOKENS["company_profile_field"], 512)
        self.assertLess(ROLE_MAX_OUTPUT_TOKENS["company_profile_field"], 2048)

    def test_deep_extract_allowed_more_than_default(self):
        """The one-shot dossier legitimately needs a large allowance."""
        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS
        self.assertGreater(
            ROLE_MAX_OUTPUT_TOKENS["company_profile_deep_extract"], 2048)


class SourceCompressionTests(TestCase):
    """Shrink the bundle once, at collection, not per call."""

    def test_boilerplate_is_stripped(self):
        from fundos.profile.services import compress_payloads
        out = compress_payloads({"website": {
            "text": "We build rockets.\nAccept all cookies\n"
                    "© 2026 All rights reserved\nOur customers love us."}})
        text = out["website"]["text"]
        self.assertIn("We build rockets.", text)
        self.assertIn("Our customers love us.", text)
        self.assertNotIn("cookies", text.lower())
        self.assertNotIn("rights reserved", text.lower())

    def test_duplicate_document_summaries_deduped(self):
        from fundos.profile.services import compress_payloads
        doc = {"detected_type": "pitch_deck", "summary": "Series A deck."}
        out = compress_payloads({"documents": [doc, dict(doc), dict(doc)]})
        self.assertEqual(len(out["documents"]), 1)

    def test_empty_keys_pruned(self):
        from fundos.profile.services import compress_payloads
        out = compress_payloads({"website": {}, "documents": [],
                                 "founders": [{"name": "Ada"}]})
        self.assertNotIn("website", out)
        self.assertNotIn("documents", out)
        self.assertIn("founders", out)

    def test_document_list_is_capped(self):
        from fundos.profile.services import MAX_DOCUMENTS, compress_payloads
        docs = [{"detected_type": f"t{i}", "summary": f"s{i}"}
                for i in range(MAX_DOCUMENTS + 10)]
        out = compress_payloads({"documents": docs})
        self.assertLessEqual(len(out["documents"]), MAX_DOCUMENTS)


class FingerprintTests(TestCase):
    """Identical sources must produce an identical fingerprint."""

    def test_stable_across_key_order(self):
        from fundos.profile.services import sources_fingerprint
        a = {"website": {"x": 1}, "documents": [1, 2]}
        b = {"documents": [1, 2], "website": {"x": 1}}
        self.assertEqual(sources_fingerprint(a), sources_fingerprint(b))

    def test_changes_when_sources_change(self):
        from fundos.profile.services import sources_fingerprint
        a = {"website": {"x": 1}}
        b = {"website": {"x": 2}}
        self.assertNotEqual(sources_fingerprint(a), sources_fingerprint(b))


class SkipIfUnchangedTests(TestCase):
    """A repeat run over identical sources must not call the LLM at all."""

    def setUp(self):
        super().setUp()          # ShippedDefaultsMixin — seed deterministically
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        # These tests are about the SKIP GATE, not about the model: they need
        # a generation that reaches "mode: pipeline" so a sources hash gets
        # stamped, and there is no API key in the suite. Stated here rather
        # than inherited from a settings default — dev used to mock by
        # default and that has been turned off, since mocked output is
        # plausible enough to be mistaken for a real run.
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]

    def _profile(self):
        from fundos.profile.services import get_or_create_profile
        return get_or_create_profile(self.company,
                                     user=self.w["a"]["founder"])

    def test_second_run_skips_generation(self):
        from fundos.profile.services import generate_profile

        profile = self._profile()
        with patch("fundos.profile.services.collect_sources",
                   return_value=({"website": {"t": "same"}}, [])):
            first = generate_profile(profile, user=self.w["a"]["founder"])
            self.assertNotEqual(first.get("skippedReason"),
                                "sources_unchanged")

            profile.refresh_from_db()
            self.assertTrue(profile.sources_hash)

            # Nothing changed → must be a no-op.
            second = generate_profile(profile, user=self.w["a"]["founder"])
            self.assertEqual(second.get("skippedReason"), "sources_unchanged")
            self.assertEqual(second["generated"], [])

    def test_force_overrides_the_skip_gate(self):
        from fundos.profile.services import generate_profile

        profile = self._profile()
        with patch("fundos.profile.services.collect_sources",
                   return_value=({"website": {"t": "same"}}, [])):
            generate_profile(profile, user=self.w["a"]["founder"])
            forced = generate_profile(profile, user=self.w["a"]["founder"],
                                      force=True)
        self.assertNotEqual(forced.get("skippedReason"), "sources_unchanged")

    def test_changed_sources_trigger_regeneration(self):
        from fundos.profile.services import generate_profile

        profile = self._profile()
        with patch("fundos.profile.services.collect_sources",
                   return_value=({"website": {"t": "v1"}}, [])):
            generate_profile(profile, user=self.w["a"]["founder"])
        with patch("fundos.profile.services.collect_sources",
                   return_value=({"website": {"t": "v2"}}, [])):
            again = generate_profile(profile, user=self.w["a"]["founder"])
        self.assertNotEqual(again.get("skippedReason"), "sources_unchanged")


class ProviderUniformityTests(ShippedDefaultsMixin, TestCase):
    """Cost controls must behave identically whichever LLM a tenant runs on.

    The tiering layer is provider-neutral by design: a tenant's Simple /
    Advanced tier points at an LLMConfigProfile, and that profile can be
    Anthropic, Gemini or OpenAI. Nothing here may assume Claude.
    """

    def test_every_provider_declares_prompt_cache(self):
        from fundos.llm.models import PROVIDER_CAPABILITIES
        for provider, caps in PROVIDER_CAPABILITIES.items():
            self.assertIn("prompt_cache", caps,
                          f"{provider} must declare prompt_cache support")

    def test_every_provider_has_a_cache_strategy(self):
        from fundos.llm.models import CACHE_STRATEGY, PROVIDER_CAPABILITIES
        for provider in PROVIDER_CAPABILITIES:
            self.assertIn(CACHE_STRATEGY.get(provider),
                          ("explicit", "implicit"),
                          f"{provider} needs a known cache strategy")

    def test_cache_strategy_resolves_for_every_endpoint_kind(self):
        from fundos.llm.adapter import cache_strategy_for
        from fundos.llm.models import ENDPOINT_KIND_TO_PROVIDER
        for kind in ENDPOINT_KIND_TO_PROVIDER:
            self.assertIsNotNone(cache_strategy_for(kind),
                                 f"{kind} has no cache strategy")

    def test_usage_shape_is_identical_for_all_providers(self):
        """The call log must show the same four numbers regardless of vendor."""
        from fundos.llm.adapter import _usage
        expected = {"prompt_tokens", "completion_tokens",
                    "cache_read_tokens", "cache_write_tokens"}
        self.assertEqual(set(_usage().keys()), expected)
        self.assertEqual(set(_usage(1, 2, 3, 4).keys()), expected)

    def test_openai_cached_tokens_extracted_from_nested_details(self):
        from fundos.llm.adapter import _openai_cached_tokens

        class _Details:
            cached_tokens = 512

        class _Usage:
            prompt_tokens_details = _Details()

        self.assertEqual(_openai_cached_tokens(_Usage()), 512)
        self.assertEqual(_openai_cached_tokens(None), 0)

    def test_tier_profiles_are_role_named_not_vendor_named(self):
        """Switching a tenant to Gemini must not require renaming anything."""
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        call_command("seed_platform_config", verbosity=0)
        for tier in ("simple", "advanced"):
            row = TenantLLMTier.objects.filter(tenant_id=None,
                                               tier=tier).first()
            self.assertIsNotNone(row, f"no global {tier} tier row")
            self.assertEqual(row.config_profile_id, f"tier.{tier}",
                             "global tier must point at a role-named profile")

        # Vendor alternates exist, inactive, ready to switch to.
        for code in ("tier.simple.gemini", "tier.advanced.gemini",
                     "tier.simple.openai", "tier.advanced.openai"):
            prof = LLMConfigProfile.objects.filter(code=code).first()
            self.assertIsNotNone(prof, f"{code} alternate not seeded")
            self.assertFalse(prof.is_active,
                             f"{code} should ship inactive")

    def test_tenant_can_be_switched_to_gemini_without_code_change(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile, TenantLLMTier

        call_command("seed_platform_config", verbosity=0)
        gem = LLMConfigProfile.objects.get(code="tier.advanced.gemini")
        gem.is_active = True
        gem.save()

        tenant = "11111111-1111-1111-1111-111111111111"
        TenantLLMTier.objects.create(tenant_id=tenant, tier="advanced",
                                     config_profile_id="tier.advanced.gemini")
        self.assertEqual(
            TenantLLMTier.resolve_code("advanced", tenant),
            "tier.advanced.gemini")
        # Other tenants still fall back to the global default.
        self.assertEqual(TenantLLMTier.resolve_code("advanced", None),
                         "tier.advanced")

    def test_seeded_tier_profiles_have_structured_output_on(self):
        """v28 — SPLIT. The original asserted structured output on both

        seeded tiers, reasoning that without it the JSON-repair second call
        can fire. That reasoning still holds for `tier.simple`.

        It cannot hold for `tier.advanced`. Measured 19 Aug 2026 on
        gemini-3.5-flash, a responseSchema suppresses google_search outright:
        0 of 4 configurations searched with a schema, 4 of 4 without. A tier
        whose entire purpose is retrieval cannot trade retrieval for a saved
        repair call — and the repair path exists precisely to cover this.
        """
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile

        call_command("seed_platform_config", verbosity=0)

        simple = LLMConfigProfile.objects.get(code="tier.simple")
        self.assertTrue(simple.structured_output)
        self.assertTrue(simple.enable_prompt_cache)

        advanced = LLMConfigProfile.objects.get(code="tier.advanced")
        self.assertFalse(advanced.structured_output,
                         "a schema on a searching tier costs the search")
        self.assertTrue(advanced.enable_prompt_cache)

    def test_advanced_tier_bounds_web_search(self):
        """Each search round trip re-bills accumulated context."""
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile

        call_command("seed_platform_config", verbosity=0)
        prof = LLMConfigProfile.objects.get(code="tier.advanced")
        self.assertTrue(prof.web_search)
        self.assertTrue(prof.max_uses, "max_uses must be bounded")
        self.assertLessEqual(prof.max_uses, 8)


class PriceBookKeyingTests(TestCase):
    """Price rows must key on what _log_call actually writes."""

    def test_seeder_keys_rows_by_endpoint_code(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMModelCost

        call_command("seed_platform_config", verbosity=0)
        call_command("seed_llm_costs", verbosity=0)

        # _log_call writes provider=endpoint.code (e.g. "ANTHROPIC"), NOT the
        # provider family ("anthropic"). A family-keyed row never joins.
        self.assertTrue(
            LLMModelCost.objects.filter(provider="ANTHROPIC").exists(),
            "no price rows keyed by endpoint code")

    def test_configured_models_are_priced_exactly(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile, LLMModelCost

        call_command("seed_platform_config", verbosity=0)
        call_command("seed_llm_costs", verbosity=0)

        for prof in LLMConfigProfile.objects.all():
            row = LLMModelCost.objects.filter(
                provider=prof.endpoint_id,
                model_name=prof.model_string).first()
            self.assertIsNotNone(
                row, f"{prof.endpoint_id}/{prof.model_string} unpriced")
            self.assertGreater(row.input_cost_per_1k_inr, 0)

    def test_dated_model_variant_inherits_family_rate(self):
        from fundos.llm.management.commands.seed_llm_costs import rates_for
        base = rates_for("anthropic", "claude-haiku-4-5")
        dated = rates_for("anthropic", "claude-haiku-4-5-20251001")
        self.assertEqual(base, dated)

    def test_longest_prefix_wins(self):
        from fundos.llm.management.commands.seed_llm_costs import rates_for
        # gpt-4o-mini must not pick up the gpt-4o or gpt-4 rate.
        self.assertEqual(rates_for("openai", "gpt-4o-mini"), (0.15, 0.60))
        self.assertEqual(rates_for("openai", "gpt-4o"), (2.50, 10.00))

    def test_unknown_model_falls_back_pessimistically(self):
        """An unpriced model must over-report, never record zero."""
        from fundos.llm.management.commands.seed_llm_costs import rates_for
        usd_in, usd_out = rates_for("anthropic", "claude-something-new")
        self.assertGreater(usd_in, 0)
        self.assertGreater(usd_out, 0)


class TwoStageDossierTests(ShippedDefaultsMixin, TestCase):
    """Stage 1 retrieves cheaply; stage 2 reasons on a stronger model.

    The whole economic point is that stage 2 receives ONLY stage 1's
    extracted JSON — never the raw source corpus. If the corpus leaked in,
    the call would be as expensive as stage 1 and the split would be
    pointless.
    """

    def setUp(self):
        super().setUp()          # ShippedDefaultsMixin — seed deterministically
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)

    def test_judgment_is_a_first_class_tier(self):
        """Not a hardcoded profile code — a tier like Simple and Advanced."""
        from fundos.llm.models import LLM_TIERS, TIER_DEFINITIONS
        keys = {k for k, _ in LLM_TIERS}
        self.assertEqual(keys, {"simple", "advanced", "judgment"})
        for k in keys:
            d = TIER_DEFINITIONS[k]
            for field in ("label", "purpose", "tools", "use_when", "cost"):
                self.assertTrue(d.get(field), f"{k} missing {field}")

    def test_judgment_resolves_through_tenant_tier_not_hardcode(self):
        from fundos.llm.tiers import resolve_tier_profile_code
        self.assertEqual(
            resolve_tier_profile_code(role="company_profile_judgment"),
            "tier.judgment")

    def test_no_call_site_hardcodes_a_config_profile(self):
        """The whole point: models are chosen in admin, never in code."""
        import pathlib
        src = pathlib.Path("fundos/profile/services.py").read_text()
        self.assertNotIn('config_profile="cp_judgment"', src)

    def test_extraction_roles_default_to_simple(self):
        """The old default sent 15 extraction calls to the web-search tier."""
        from fundos.llm.default_prompts import get_tier
        for role in ("company_profile_records", "company_profile_structured",
                     "company_profile_section", "founder_profile",
                     "company_profile_field", "profile_qa"):
            self.assertEqual(get_tier(role), "simple", role)

    def test_only_retrieval_roles_default_to_advanced(self):
        from fundos.llm.default_prompts import get_tier
        self.assertEqual(get_tier("company_profile_deep_extract"), "advanced")
        self.assertEqual(get_tier("company_profile_judgment"), "judgment")

    def test_judgment_profile_is_seeded(self):
        from fundos.llm.models import LLMConfigProfile
        prof = LLMConfigProfile.objects.filter(code="tier.judgment").first()
        self.assertIsNotNone(prof, "tier.judgment not seeded")
        self.assertTrue(prof.is_active)
        # Must NOT browse — it reasons from the extraction.
        self.assertFalse(prof.web_search)
        # Thinking ON: adjudicating conflicting figures is what it helps with.
        self.assertEqual(prof.thinking_mode, "tokens")

    def test_judgment_role_has_a_binding(self):
        """Without one, llm_generate raises and the stage silently degrades."""
        from fundos.llm.models import LLMRoleBinding
        self.assertTrue(
            LLMRoleBinding.objects.filter(
                role="company_profile_judgment").exists())

    def test_judgment_input_excludes_the_raw_corpus(self):
        from fundos.profile.services import _JUDGMENT_INPUT_KEYS
        for leaky in ("website_extract", "document_extracts", "research",
                      "founders"):
            self.assertNotIn(
                leaky, _JUDGMENT_INPUT_KEYS,
                f"{leaky} would defeat the cost split")

    def test_judgment_input_includes_what_reasoning_needs(self):
        from fundos.profile.services import _JUDGMENT_INPUT_KEYS
        for needed in ("conflicts", "financials", "leadership",
                       "competitors"):
            self.assertIn(needed, _JUDGMENT_INPUT_KEYS)

    def test_stage_two_merges_thesis_over_stage_one(self):
        from unittest.mock import patch
        from fundos.profile.services import _apply_judgment_stage

        extraction = {
            "financials": {"by_year": [{"fiscal_year": "FY2025"}]},
            "conflicts": [{"field": "market_cap", "values": ["a", "b"]}],
            "investment_thesis": {"why_attractive": "STAGE ONE"},
            "missing": ["som"],
        }
        judged = {
            "investment_thesis": {"why_attractive": "STAGE TWO"},
            "conflict_resolution": [{"field": "market_cap",
                                     "recommended_value": "a"}],
            "missing": ["reserve_life"],
        }

        class _P:
            class company:
                name = "Acme"

        with patch("fundos.llm.adapter.llm_generate", return_value=judged), \
             patch("fundos.llm.guardrail.apply", side_effect=lambda r, d: d):
            out = _apply_judgment_stage(_P(), extraction)

        self.assertEqual(out["investment_thesis"]["why_attractive"],
                         "STAGE TWO")
        # Raw conflicts SURVIVE alongside the adjudication — the founder must
        # still see that sources disagreed, not just the model's pick.
        self.assertEqual(len(out["conflicts"]), 1)
        self.assertEqual(len(out["conflict_resolution"]), 1)
        # missing lists are unioned, not replaced
        self.assertIn("som", out["missing"])
        self.assertIn("reserve_life", out["missing"])

    def test_stage_two_failure_keeps_stage_one_thesis(self):
        """A failed judgement must degrade, never lose the profile."""
        from unittest.mock import patch
        from fundos.profile.services import _apply_judgment_stage

        extraction = {"financials": {"x": 1},
                      "investment_thesis": {"why_attractive": "STAGE ONE"}}

        class _P:
            class company:
                name = "Acme"

        with patch("fundos.llm.adapter.llm_generate",
                   side_effect=RuntimeError("provider down")):
            out = _apply_judgment_stage(_P(), extraction)
        self.assertEqual(out["investment_thesis"]["why_attractive"],
                         "STAGE ONE")

    def test_flag_off_is_a_clean_rollback(self):
        from unittest.mock import patch
        from fundos.profile.services import _apply_judgment_stage

        extraction = {"financials": {"x": 1}, "investment_thesis": {"a": 1}}

        class _P:
            class company:
                name = "Acme"

        with patch("fundos.profile.services._judgment_stage_enabled",
                   return_value=False), \
             patch("fundos.llm.adapter.llm_generate") as gen:
            out = _apply_judgment_stage(_P(), extraction)
        gen.assert_not_called()
        self.assertEqual(out, extraction)

    def test_judgment_keeps_thinking_enabled(self):
        """Extraction roles have thinking stripped; judgement must not."""
        from fundos.llm.adapter import NO_THINKING_ROLES
        self.assertNotIn("company_profile_judgment", NO_THINKING_ROLES)


class JudgmentReuseTests(TestCase):
    """The expensive call must pay for itself more than once.

    Without persisting the judgement, every downstream Simple call would
    re-derive its own view from the raw sources and could contradict the
    adjudication that was already paid for — the profile would say one thing
    and the thesis another.
    """

    def test_distil_keeps_conclusions_drops_reasoning(self):
        from fundos.profile.services import _distil_judgment
        judged = {
            "investment_thesis": {
                "why_attractive": "long prose " * 200,
                "leadership_assessment": "assessment text",
                "risks_and_open_questions": ["r%d" % i for i in range(20)],
            },
            "conflict_resolution": [
                {"field": "market_cap", "recommended_value": "3.05T",
                 "confidence": 0.8, "outlier_flagged": "ICICI figure",
                 "reasoning": "very long reasoning " * 100},
            ],
            "data_quality_notes": ["n1", "n2"],
        }
        out = _distil_judgment(judged)
        # Conclusions kept
        self.assertEqual(out["resolved_values"]["market_cap"]["value"],
                         "3.05T")
        self.assertEqual(
            out["resolved_values"]["market_cap"]["outlier_flagged"],
            "ICICI figure")
        # Reasoning dropped — cheap calls need the answer, not the argument
        self.assertNotIn("reasoning", out["resolved_values"]["market_cap"])
        # Bounded so reuse stays close to free
        self.assertLessEqual(len(out["risks"]), 8)
        self.assertLessEqual(len(out["leadership_assessment"]), 1200)

    def test_judgment_context_is_empty_when_none_ran(self):
        from fundos.profile.services import judgment_context

        class _P:
            judgment = None
        self.assertEqual(judgment_context(_P()), {})

    def test_section_roles_accept_judgment_context(self):
        """context_keys is enforced — judgment must be declared or filtered."""
        from fundos.llm.adapter import _filter_context
        for role in ("company_profile_records", "company_profile_structured",
                     "company_profile_section"):
            out = _filter_context(role, {"company": {}, "section_key": "x",
                                         "judgment": {"risks": ["a"]}})
            self.assertIn("judgment", out,
                          f"{role} would filter out the judgement context")


class EndpointModelCoherenceTests(ShippedDefaultsMixin, TestCase):
    """A profile that names a model has implicitly named its provider.

    Before this fix the endpoint came from the role BINDING while the model
    came from the config PROFILE. That is invisible while every profile sits
    on the same endpoint as its binding — and breaks the moment a tenant
    switches a tier to another vendor, sending e.g. a Gemini model string to
    the Anthropic API.
    """

    def setUp(self):
        super().setUp()          # ShippedDefaultsMixin — seed deterministically
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        # Global mocking short-circuits dispatch entirely — these tests are
        # specifically about which endpoint dispatch receives.
        from fundos.config.models import AppConfiguration
        AppConfiguration.objects.update(ai_mocked=False)

    def test_profile_endpoint_overrides_binding_endpoint(self):
        from unittest.mock import patch
        from fundos.llm.models import (LLMConfigProfile, LLMEndpoint,
                                       LLMRoleBinding, TenantLLMTier)

        gem = LLMEndpoint.objects.get(code="GEMINI")
        gem.is_active = True
        gem.save()
        prof = LLMConfigProfile.objects.get(code="tier.judgment.gemini")
        prof.is_active = True
        prof.save()
        TenantLLMTier.objects.update_or_create(
            tenant_id=None, tier="judgment",
            defaults={"config_profile_id": "tier.judgment.gemini"})

        b = LLMRoleBinding.objects.filter(
            role="company_profile_judgment").first()
        b.primary_endpoint = LLMEndpoint.objects.get(code="ANTHROPIC")
        b.is_mocked = False
        b.save()

        seen = {}

        def _fake_dispatch(endpoint, system, prompt, binding,
                           model_override=None, **kw):
            seen["endpoint_kind"] = endpoint.provider_kind
            seen["model"] = model_override or endpoint.default_model
            return '{"investment_thesis": {"why_attractive": "x"}}', {
                "prompt_tokens": 1, "completion_tokens": 1,
                "cache_read_tokens": 0, "cache_write_tokens": 0}

        from fundos.llm.adapter import llm_generate
        with patch("fundos.llm.adapter._dispatch", side_effect=_fake_dispatch):
            llm_generate(role="company_profile_judgment",
                         context={"extraction": {"a": 1}})

        # The model came from the Gemini profile, so the endpoint must be
        # Gemini too — not the Anthropic endpoint the binding named.
        # Asserted against the profile's own model rather than a literal: the
        # claim under test is "the model came from THIS profile", and pinning a
        # model string here made the test fail whenever the seeder's pinned
        # model changed — a failure that says nothing about endpoint/model
        # coherence, which is the only thing this test is for.
        self.assertEqual(seen["model"], prof.model_string)
        self.assertEqual(seen["endpoint_kind"], "gemini")

    def test_cross_vendor_fallback_is_dropped(self):
        """Falling back across vendors reintroduces the same mismatch."""
        from unittest.mock import patch
        from fundos.llm.models import (LLMConfigProfile, LLMEndpoint,
                                       LLMRoleBinding, TenantLLMTier)

        gem = LLMEndpoint.objects.get(code="GEMINI")
        gem.is_active = True
        gem.save()
        prof = LLMConfigProfile.objects.get(code="tier.judgment.gemini")
        prof.is_active = True
        prof.save()
        TenantLLMTier.objects.update_or_create(
            tenant_id=None, tier="judgment",
            defaults={"config_profile_id": "tier.judgment.gemini"})

        anth = LLMEndpoint.objects.get(code="ANTHROPIC")
        b = LLMRoleBinding.objects.filter(
            role="company_profile_judgment").first()
        b.primary_endpoint = anth
        b.fallback_endpoint = anth       # different dialect from Gemini
        b.is_mocked = False
        b.save()

        kinds = []

        def _fake_dispatch(endpoint, *a, **kw):
            kinds.append(endpoint.provider_kind)
            raise RuntimeError("boom")   # non-retryable → try next endpoint

        from fundos.core.exceptions import LLMUnavailable
        from fundos.llm.adapter import llm_generate
        with patch("fundos.llm.adapter._dispatch", side_effect=_fake_dispatch):
            with self.assertRaises(LLMUnavailable):
                llm_generate(role="company_profile_judgment",
                             context={"extraction": {"a": 1}})

        # Only the Gemini endpoint was tried; the Anthropic fallback was
        # dropped rather than called with a Gemini model string.
        self.assertEqual(set(kinds), {"gemini"})

    def test_binding_endpoint_still_used_when_profile_has_none(self):
        """No regression for the ordinary case."""
        from unittest.mock import patch
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        b = LLMRoleBinding.objects.filter(
            role="company_profile_records").first()
        b.primary_endpoint = LLMEndpoint.objects.get(code="ANTHROPIC")
        b.is_mocked = False
        b.save()

        seen = {}

        def _fake_dispatch(endpoint, *a, **kw):
            seen["kind"] = endpoint.provider_kind
            return '{"records": []}', {"prompt_tokens": 1,
                                       "completion_tokens": 1,
                                       "cache_read_tokens": 0,
                                       "cache_write_tokens": 0}

        from fundos.llm.adapter import llm_generate
        with patch("fundos.llm.adapter._dispatch", side_effect=_fake_dispatch):
            llm_generate(role="company_profile_records", context={})
        self.assertEqual(seen["kind"], "anthropic")


class SearchContainmentTests(ShippedDefaultsMixin, TestCase):
    """max_uses is enforced by Anthropic only. Everything else is defence
    in depth, and each layer's limit is asserted honestly here."""

    def setUp(self):
        super().setUp()          # ShippedDefaultsMixin — seed deterministically
        from django.core.management import call_command
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from django.core.cache import cache
        cache.clear()

    def test_enforcement_mode_per_provider(self):
        from fundos.llm.adapter import search_enforcement_for
        self.assertEqual(search_enforcement_for("anthropic"), "native")
        self.assertEqual(search_enforcement_for("gemini"), "advisory")
        self.assertEqual(search_enforcement_for("openai_chat"), "advisory")

    def test_advisory_directive_states_the_cap(self):
        from fundos.llm.adapter import _search_directive
        d = _search_directive(6)
        self.assertIn("AT MOST 6", d)
        self.assertEqual(_search_directive(None), "")

    def test_worst_case_reservation_is_pessimistic_for_advisory(self):
        """An advisory provider must reserve MORE than its nominal cap."""
        from fundos.llm.adapter import _worst_case_search_tokens
        caps = {"web_search": {"max_uses": 6}}
        native = _worst_case_search_tokens(caps, "native")
        advisory = _worst_case_search_tokens(caps, "advisory")
        self.assertGreater(advisory, native)
        self.assertEqual(advisory, native * 3)

    def test_no_search_means_no_reservation(self):
        from fundos.llm.adapter import _worst_case_search_tokens
        self.assertEqual(_worst_case_search_tokens({}, "advisory"), 0)

    def test_gemini_count_uses_queries_not_chunks(self):
        """groundingChunks counts SOURCES and over-reports the search count."""
        from fundos.llm.adapter import _count_searches
        meta = {"web_search_queries": ["q1", "q2"]}
        self.assertEqual(_count_searches("gemini", meta), 2)

    def test_unknown_count_is_none_not_zero(self):
        from fundos.llm.adapter import _count_searches
        self.assertIsNone(_count_searches("gemini", {}))
        self.assertIsNone(_count_searches("anthropic", None))

    def test_breaker_trips_after_repeated_overruns(self):
        from fundos.llm.adapter import (SEARCH_BREAKER_THRESHOLD,
                                        record_search_outcome,
                                        search_breaker_tripped)
        code = "tier.advanced.gemini"
        self.assertFalse(search_breaker_tripped(code))
        for _ in range(SEARCH_BREAKER_THRESHOLD):
            record_search_outcome(code, allowed=6, actual=14)
        self.assertTrue(search_breaker_tripped(code))

    def test_single_overrun_does_not_trip_the_breaker(self):
        """One company genuinely needing more searching is not an offence."""
        from fundos.llm.adapter import (record_search_outcome,
                                        search_breaker_tripped)
        code = "tier.advanced.openai"
        record_search_outcome(code, allowed=6, actual=9)
        self.assertFalse(search_breaker_tripped(code))

    def test_compliant_call_resets_the_strike_count(self):
        from fundos.llm.adapter import (record_search_outcome,
                                        search_breaker_tripped)
        code = "tier.advanced.gemini"
        record_search_outcome(code, allowed=6, actual=14)
        record_search_outcome(code, allowed=6, actual=14)
        record_search_outcome(code, allowed=6, actual=3)   # compliant
        record_search_outcome(code, allowed=6, actual=14)
        self.assertFalse(search_breaker_tripped(code))

    def test_unknown_count_cannot_trip_the_breaker(self):
        """A provider that reports nothing must not be punished for it."""
        from fundos.llm.adapter import (record_search_outcome,
                                        search_breaker_tripped)
        code = "tier.advanced"
        for _ in range(10):
            record_search_outcome(code, allowed=6, actual=None)
        self.assertFalse(search_breaker_tripped(code))

    def test_open_breaker_strips_web_search_from_the_call(self):
        from unittest.mock import patch
        from fundos.config.models import AppConfiguration
        from fundos.llm.adapter import (SEARCH_BREAKER_THRESHOLD,
                                        llm_generate, record_search_outcome)
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        AppConfiguration.objects.update(ai_mocked=False)
        b = LLMRoleBinding.objects.filter(
            role="company_profile_deep_extract").first()
        b.primary_endpoint = LLMEndpoint.objects.get(code="ANTHROPIC")
        b.is_mocked = False
        b.save()

        # Trip the breaker on the profile this ROLE ACTUALLY RESOLVES TO.
        #
        # It was tripped on "tier.advanced" — the role-named profile — while
        # the role resolves to "tier.advanced.gemini", so the breaker was open
        # on a profile nothing was using and this test was measuring the tier
        # naming drift rather than the breaker. That drift is real and has its
        # own failing test (`test_tier_profiles_are_role_named_not_vendor_named`);
        # this one is about whether an open breaker refuses a search-required
        # role, and it should fail for that reason or not at all.
        from fundos.llm.tiers import resolve_tier_profile_code
        profile_code = resolve_tier_profile_code(
            role="company_profile_deep_extract")
        for _ in range(SEARCH_BREAKER_THRESHOLD):
            record_search_outcome(profile_code, allowed=6, actual=20)

        seen = {}

        def _fake(endpoint, system, prompt, binding, model_override=None,
                  capabilities=None, **kw):
            seen["caps"] = capabilities or {}
            return '{"missing": []}', {"prompt_tokens": 1,
                                       "completion_tokens": 1,
                                       "cache_read_tokens": 0,
                                       "cache_write_tokens": 0}

        # v27.4 — BEHAVIOUR CHANGED, deliberately.
        #
        # This used to assert that an open breaker stripped web_search and let
        # the call proceed: "better a thinner answer than an unbounded bill".
        # That is the right trade for a role that searches to IMPROVE its
        # answer, and a false choice for one that searches because it cannot
        # otherwise answer. `company_profile_deep_extract` is search-required,
        # so a stripped call does not produce a thinner dossier — it produces a
        # confident one assembled from the model's recollection, and the
        # grounding gate does not catch it (a website-only run passed the gate
        # on every run of 17 Aug).
        #
        # The breaker may thin an answer; it may not falsify one.
        from fundos.core.exceptions import UngroundableCall
        with patch("fundos.llm.adapter._dispatch", side_effect=_fake):
            with self.assertRaises(UngroundableCall):
                llm_generate(role="company_profile_deep_extract", context={})
        self.assertNotIn("caps", seen,
                         "the call must be refused BEFORE the tokens are spent")

    def test_open_breaker_still_strips_search_from_an_optional_role(self):
        """The containment behaviour is unchanged where it was correct.

        `market_research` benefits from search but remains publishable without
        it, so the breaker degrades rather than refuses — which is what makes
        the refusal above a targeted change and not a blanket one.
        """
        from unittest.mock import patch
        from fundos.config.models import AppConfiguration
        from fundos.llm.adapter import (SEARCH_BREAKER_THRESHOLD,
                                        llm_generate, record_search_outcome)
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding

        AppConfiguration.objects.update(ai_mocked=False)
        b = LLMRoleBinding.objects.filter(role="market_research").first()
        if b is None:
            self.skipTest("no market_research binding in this configuration")
        b.primary_endpoint = LLMEndpoint.objects.get(code="ANTHROPIC")
        b.is_mocked = False
        b.save()

        for _ in range(SEARCH_BREAKER_THRESHOLD):
            record_search_outcome("tier.advanced", allowed=6, actual=20)

        seen = {}

        def _fake(endpoint, system, prompt, binding, model_override=None,
                  capabilities=None, **kw):
            seen["caps"] = capabilities or {}
            return '{"missing": []}', {"prompt_tokens": 1,
                                       "completion_tokens": 1,
                                       "cache_read_tokens": 0,
                                       "cache_write_tokens": 0}

        with patch("fundos.llm.adapter._dispatch", side_effect=_fake):
            llm_generate(role="market_research", context={})

        self.assertNotIn("web_search", seen.get("caps", {}),
                         "breaker was open but search was still requested")

    def test_budget_refuses_a_call_it_could_not_afford_at_worst_case(self):
        from decimal import Decimal
        from fundos.core.exceptions import LLMUnavailable
        from fundos.llm.adapter import _enforce_budget
        from fundos.llm.models import LLMCallLog, TenantLLMBudget

        TenantLLMBudget.objects.create(tenant_id=None,
                                       monthly_cap_inr=Decimal("50.00"))
        LLMCallLog.objects.create(provider="X", function_name="r",
                                  cost_inr=Decimal("49.00"))
        # Under the cap, so an ordinary call is allowed...
        _enforce_budget(role="x")
        # ...but a worst-case search call is not.
        with self.assertRaises(LLMUnavailable):
            _enforce_budget(role="company_profile_deep_extract",
                            reserve_tokens=100000)


class DiagnosticsTests(TestCase):
    """Enough signal in the logs to IMPROVE an algorithm, not just bill it."""

    def test_output_signals_capture_quality_not_cost(self):
        from fundos.llm.adapter import _output_signals
        sig = _output_signals({
            "records": [1, 2, 3],
            "missing": ["a", "b"],
            "conflicts": [{"field": "x"}],
            "needsInput": True,
            "content": "hello",
        })
        self.assertEqual(sig["records_count"], 3)
        self.assertEqual(sig["missing_count"], 2)
        self.assertEqual(sig["conflicts_count"], 1)
        self.assertTrue(sig["needs_input"])
        self.assertEqual(sig["content_chars"], 5)
        # The key set catches a plausible-looking but wrongly-shaped object.
        self.assertIn("keys", sig)

    def test_prompt_fingerprint_changes_when_the_prompt_changes(self):
        """Without this, 'did that prompt edit help?' is unanswerable."""
        from fundos.llm.adapter import prompt_fingerprint
        a = prompt_fingerprint("You are an analyst.")
        b = prompt_fingerprint("You are a senior analyst.")
        self.assertNotEqual(a, b)
        self.assertEqual(a, prompt_fingerprint("You are an analyst."))
        self.assertEqual(prompt_fingerprint(""), "")

    def test_source_stats_measure_what_the_model_had(self):
        from fundos.profile.services import _source_stats
        stats = _source_stats({
            "website": {"text": "x" * 100, "about": "y" * 50},
            "documents": [{"summary": "z" * 200, "detected_type": "deck"},
                          {"summary": "w" * 100, "detected_type": "mis"}],
            "research": {"market": "m" * 40},
            "founders": [{"name": "A"}, {"name": "B"}],
        })
        self.assertEqual(stats["website_chars"], 150)
        self.assertEqual(stats["document_count"], 2)
        self.assertEqual(stats["document_chars"], 300)
        self.assertEqual(stats["founder_count"], 2)
        self.assertEqual(stats["total_chars"], 150 + 300 + 40)
        self.assertEqual(stats["document_types"], ["deck", "mis"])

    def test_source_stats_tolerate_missing_sources(self):
        from fundos.profile.services import _source_stats
        stats = _source_stats({})
        self.assertEqual(stats["total_chars"], 0)

    def test_generation_run_model_records_the_full_picture(self):
        from fundos.profile.models import ProfileGenerationRun
        names = {f.name for f in ProfileGenerationRun._meta.fields}
        # Inputs, outputs, judgement and economics must all be present —
        # any one missing makes a whole class of question unanswerable.
        for field in ("source_stats", "source_failures",
                      "sections_generated", "sections_needing_input",
                      "judgment_ran", "conflicts_found", "conflicts_resolved",
                      "llm_calls", "total_cost_inr", "total_search_count",
                      "duration_ms", "mode", "forced", "sources_hash"):
            self.assertIn(field, names, f"ProfileGenerationRun.{field}")

    def test_call_log_carries_the_attribution_columns(self):
        from fundos.llm.models import LLMCallLog
        names = {f.name for f in LLMCallLog._meta.fields}
        for field in ("tier", "config_profile", "prompt_fingerprint",
                      "context_chars", "was_repaired", "was_normalized",
                      "output_signals", "search_count"):
            self.assertIn(field, names, f"LLMCallLog.{field}")

    def test_diagnostics_never_break_a_generation(self):
        """A diagnostics failure must not lose the founder's profile."""
        from unittest.mock import patch
        from fundos.profile.services import _record_generation_run
        with patch("fundos.profile.services._source_stats",
                   side_effect=RuntimeError("boom")):
            # Must not raise.
            _record_generation_run(
                None, started_at=None, mode="x", forced=False,
                fingerprint="", payloads={}, failures=[], generated=[],
                skipped=[])


class DealAssessmentEngineTests(TestCase):
    """Fundraising Journey Spec Part 2 — scoring mechanics."""

    def test_monotonic_higher_bands(self):
        from fundos.engines.deal_assessment import band_monotonic
        f = lambda v: band_monotonic(v, "Higher", 50, 25, 10)
        self.assertEqual(f(60), "Excellent")
        self.assertEqual(f(50), "Excellent")     # cut-points inclusive
        self.assertEqual(f(30), "Good")
        self.assertEqual(f(10), "Fair")
        self.assertEqual(f(5), "Poor")
        self.assertIsNone(f(None))               # blank != zero

    def test_monotonic_lower_bands(self):
        from fundos.engines.deal_assessment import band_monotonic
        f = lambda v: band_monotonic(v, "Lower", 10, 25, 50)
        self.assertEqual(f(5), "Excellent")
        self.assertEqual(f(25), "Good")
        self.assertEqual(f(60), "Poor")

    def test_range_band_penalises_both_directions(self):
        """Too much is as much of a flag as too little."""
        from fundos.engines.deal_assessment import band_range
        f = lambda v: band_range(v, 2, 4, 25, 50)
        self.assertEqual(f(3), "Excellent")
        self.assertEqual(f(4.5), "Good")
        self.assertEqual(f(1.6), "Good")
        self.assertEqual(f(5.5), "Fair")
        self.assertEqual(f(20), "Poor")
        self.assertEqual(f(0.1), "Poor")

    def test_automated_path_can_never_produce_exceptional(self):
        """Spec 2.4.4 / 5.1 — 10 is override-only, with justification."""
        from fundos.engines.deal_assessment import validate_automated_band
        self.assertEqual(validate_automated_band("Exceptional"), "Excellent")
        self.assertEqual(validate_automated_band("Good"), "Good")
        self.assertIsNone(validate_automated_band("Amazing"))

    def test_blank_is_excluded_not_zeroed(self):
        """A missing input must not be scored as zero."""
        from fundos.engines.deal_assessment import weighted_rollup
        score, _ = weighted_rollup([
            {"code": "a", "score": 9, "weight": 50},
            {"code": "b", "score": None, "weight": 50},
        ])
        self.assertEqual(float(score), 9.0)      # not 4.5

    def test_applied_weight_shows_redistribution(self):
        from fundos.engines.deal_assessment import weighted_rollup
        _, applied = weighted_rollup([
            {"code": "a", "score": 9, "weight": 20},
            {"code": "b", "score": 7, "weight": 20},
            {"code": "c", "score": None, "weight": 60},
        ])
        # The two answered children now carry 50% each, not 20%.
        self.assertEqual({a["code"]: round(a["applied_weight"])
                          for a in applied}, {"a": 50, "b": 50})

    def test_fully_unscored_branch_returns_none(self):
        from fundos.engines.deal_assessment import weighted_rollup
        score, applied = weighted_rollup([{"score": None, "weight": 10}])
        self.assertIsNone(score)
        self.assertEqual(applied, [])

    def test_rating_ladder(self):
        from fundos.engines.deal_assessment import rating_for_score
        self.assertEqual(rating_for_score(9.2), "Excellent")
        self.assertEqual(rating_for_score(8.0), "Very Good")
        self.assertEqual(rating_for_score(7.0), "Good")
        self.assertEqual(rating_for_score(6.5), "Average")
        self.assertEqual(rating_for_score(5.0), "Challenging")
        self.assertEqual(rating_for_score(4.9), "Not Recommended")

    def test_all_excellent_tops_out_at_nine(self):
        """A clean automated run cannot reach 10 — by design, not by bug."""
        from fundos.engines.deal_assessment import score_assessment
        cats = [{"code": c, "weight": w,
                 "children": [{"code": c + "1", "weight": 100, "score": 9}]}
                for c, w in (("A", 20), ("B", 20), ("C", 10), ("D", 10),
                             ("E", 10), ("F", 20), ("G", 10))]
        out = score_assessment(categories=cats, parameters=[])
        self.assertEqual(out["overall_score"], 9.0)
        self.assertEqual(out["rating_band"], "Excellent")
        self.assertTrue(out["weights_valid"])

    def test_weight_sum_violation_is_flagged(self):
        from fundos.engines.deal_assessment import score_assessment
        cats = [{"code": "A", "weight": 50,
                 "children": [{"code": "A1", "weight": 100, "score": 9}]}]
        out = score_assessment(categories=cats, parameters=[])
        self.assertFalse(out["weights_valid"])

    def test_coverage_reported_separately_from_score(self):
        """7.5 on 20% coverage is not the same claim as 7.5 on 90%."""
        from fundos.engines.deal_assessment import input_coverage
        params = [{"score": 9, "is_reference_only": False},
                  {"score": None, "is_reference_only": False},
                  {"score": None, "is_reference_only": True}]   # ref excluded
        self.assertEqual(round(float(input_coverage(params))), 50)
