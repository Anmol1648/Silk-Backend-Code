"""v21 — defects from the two-company ingestion run.

Each test corresponds to something the live log showed, or to a rule the
administrator asked for after configuration was overwritten by a deploy.
"""
from unittest import mock

from django.test import TestCase


class ThinkingHeadroomTests(TestCase):
    """Every judgement call came back truncated."""

    def _config(self, budget, ceiling):
        from fundos.llm import adapter
        from fundos.llm.models import LLMEndpoint

        endpoint = LLMEndpoint(code="G", provider_kind="gemini",
                               default_model="gemini-3.5-flash",
                               api_key_env_var="GEMINI_API_KEY",
                               timeout_seconds=30)
        captured = {}

        class _Resp:
            status_code = 200
            text = "{}"

            def json(self):
                return {"candidates": [{"content": {"parts": [{"text": "{}"}]}}],
                        "usageMetadata": {}}

        def _post(url, json=None, headers=None, timeout=None, **kw):
            captured.update(json["generationConfig"])
            return _Resp()

        with mock.patch.object(adapter, "requests") as rq:
            rq.post = _post
            with mock.patch.object(type(endpoint), "api_key",
                                   mock.PropertyMock(return_value="k")):
                adapter._run_gemini(
                    endpoint, "sys", "prompt", "gemini-3.5-flash", 0.2,
                    ceiling, 30,
                    capabilities={"thinking": {"budget_tokens": budget}})
        return captured

    def test_ceiling_carries_budget_plus_a_full_answer(self):
        """A 4000-token budget under a 4096 ceiling left 96 tokens to answer
        in. The model spent 3,932 thinking and was cut off mid-JSON, while
        the call still reported as healthy."""
        cfg = self._config(budget=4000, ceiling=4096)
        self.assertEqual(cfg["thinkingConfig"], {"thinkingBudget": 4000})
        self.assertEqual(cfg["maxOutputTokens"], 8096)

    def test_thinking_off_does_not_inflate_the_ceiling(self):
        cfg = self._config(budget=0, ceiling=4096)
        self.assertEqual(cfg["maxOutputTokens"], 4096)


class FormSeedNormalisationTests(TestCase):
    """revenue_model and company_metrics were discarded on every run."""

    def test_model_key_names_are_mapped(self):
        from fundos.profile.services import _records_items_to_api

        rows, dropped = _records_items_to_api(
            "revenue_model",
            [{"name": "Retail", "share": "45%"},
             {"stream": "B2B", "percentage": "55 %"}])
        self.assertEqual(dropped, [])
        self.assertEqual(rows, [{"label": "Retail", "pct": 45.0},
                                {"label": "B2B", "pct": 55.0}])

    def test_indian_magnitudes_are_understood(self):
        from fundos.profile.services import _loose_number

        self.assertEqual(_loose_number("Rs 7,761 crore"), 7761 * 1e7)
        self.assertEqual(_loose_number("12.5 %"), 12.5)
        self.assertEqual(_loose_number("1,250"), 1250.0)
        self.assertIsNone(_loose_number("not a number"))

    def test_a_bare_string_row_is_salvaged(self):
        """'Invalid row.' fired because the row was a string, not a dict."""
        from fundos.profile.services import _records_items_to_api

        rows, dropped = _records_items_to_api(
            "revenue_model", ["Retail 45%", "Wholesale 55%"])
        self.assertEqual(dropped, [])
        self.assertEqual([r["label"] for r in rows], ["Retail", "Wholesale"])

    def test_one_bad_row_does_not_discard_the_batch(self):
        from fundos.profile.services import _records_items_to_api

        rows, dropped = _records_items_to_api(
            "company_metrics",
            [{"metric": "Employees", "amount": "1,250"},
             {"nothing": "useful"},
             {"kpi": "Plants", "value": 4}])
        self.assertEqual(len(rows), 2)
        self.assertEqual(len(dropped), 1)
        self.assertIn("no recognisable label", dropped[0])

    def test_percentages_are_rescaled_not_rejected(self):
        """The validator wants 100 ± 0.5; a model rounding to 99 is not a
        reason to lose three rows."""
        from fundos.profile.services import _records_items_to_api

        rows, _ = _records_items_to_api(
            "revenue_model",
            [{"label": "A", "pct": 33}, {"label": "B", "pct": 33},
             {"label": "C", "pct": 33}])
        self.assertAlmostEqual(sum(r["pct"] for r in rows), 100.0, places=1)


class SeederIsNonDestructiveTests(TestCase):
    """The administrator's configuration must survive a deploy."""

    def setUp(self):
        import os
        patcher = mock.patch.dict("os.environ",
                                  {"GEMINI_API_KEY": "test-key"})
        patcher.start()
        self.addCleanup(patcher.stop)
        from django.core.management import call_command
        call_command("seed_platform_config", verbosity=0)

    def test_an_inactive_tier_row_is_reactivated_without_rebuilding(self):
        """This is the exact sequence that destroyed a live configuration.

        The tier ROW was switched off. Repair treated the tier as broken,
        rebuilt the profile from the recommended table, and overwrote a
        deliberately-chosen model with one the API does not serve.
        """
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile as P, TenantLLMTier as T

        P.objects.filter(code="tier.judgment.gemini").update(
            model_string="gemini-3.5-flash")
        T.objects.filter(tenant_id=None, tier="judgment").update(
            is_active=False)

        call_command("seed_platform_config", verbosity=0)

        self.assertTrue(T.objects.get(tenant_id=None,
                                      tier="judgment").is_active)
        self.assertEqual(
            P.objects.get(code="tier.judgment.gemini").model_string,
            "gemini-3.5-flash",
            "the administrator's model choice was overwritten by a repair "
            "whose only real job was to flip a boolean")

    def test_a_catalogued_model_is_never_corrected(self):
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile as P

        P.objects.filter(code="tier.advanced.gemini").update(
            model_string="gemini-3.5-flash-lite")
        call_command("seed_platform_config", verbosity=0)
        self.assertEqual(
            P.objects.get(code="tier.advanced.gemini").model_string,
            "gemini-3.5-flash-lite")

    def test_thinking_is_not_switched_back_on(self):
        """'none' on an existing profile is a choice, not an omission."""
        from django.core.management import call_command
        from fundos.llm.models import LLMConfigProfile as P

        P.objects.filter(code="tier.judgment.gemini").update(
            thinking_mode="none", thinking_budget="")
        call_command("seed_platform_config", verbosity=0)
        self.assertEqual(
            P.objects.get(code="tier.judgment.gemini").thinking_mode, "none")

    def test_seeded_gemini_models_are_ones_the_api_serves(self):
        """One Gemini model across the system, and it is the pinned one.

        The assertion named gemini-3.5-flash while `_RECOMMENDED` and
        `PIPELINE_MODEL` both pin gemini-2.5-flash, so the test disagreed with
        the seeder it was testing. It is the pinned model that matters: the
        tiers and the two pipeline profiles must resolve to the SAME name, or
        the same run bills against two models and nothing in the record says
        which answered. Read from the source of the pin rather than restated,
        so this cannot drift again.
        """
        from fundos.llm.models import LLMConfigProfile as P
        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        pinned = Command.PIPELINE_MODEL
        self.assertEqual(pinned, "gemini-2.5-flash")
        for code in ("tier.simple.gemini", "tier.advanced.gemini",
                     "tier.judgment.gemini"):
            self.assertEqual(P.objects.get(code=code).model_string, pinned)
        for tier, model in Command._RECOMMENDED["gemini"].items():
            self.assertEqual(model, pinned, f"{tier} drifted off the pin")


class UnknownConfigProfileTests(TestCase):
    """An unresolvable code silently bypassed the whole tier system."""

    def test_the_per_section_codes_are_not_seeded(self):
        """`cp_extract.<section>` is what the section/record calls request,
        and no such profile exists — so those calls got no capabilities and
        no tier, and fell through to the endpoint default."""
        from fundos.llm.models import LLMConfigProfile
        from fundos.profile.services import _section_config_profile

        code = _section_config_profile("business_model")
        self.assertEqual(code, "cp_extract.business_model")
        self.assertFalse(LLMConfigProfile.objects.filter(code=code).exists())


class DeepExtractPromptTests(TestCase):
    """`searches` was empty on every run because nothing asked for one."""

    def test_the_prompt_asks_the_model_to_search(self):
        from fundos.llm.default_prompts import DEFAULT_PROMPTS as PROMPTS

        system = PROMPTS["company_profile_deep_extract"]["system"]
        self.assertIn("USE IT", system)
        # …without licensing recall, which is the failure mode search is
        # meant to remove rather than introduce.
        self.assertIn("does NOT license recalling figures from memory",
                      system)


class WebsiteTableTests(TestCase):
    """Financial tables were being flattened into prose."""

    HTML = ("<table><tr><th>FY</th><th>Revenue</th></tr>"
            "<tr><td>FY24</td><td>7,761 Cr</td></tr>"
            "<tr><td>FY23</td><td>7,043 Cr</td></tr>"
            "<tr><td colspan=2>Source: annual report</td></tr></table>")

    def test_rows_keep_their_shape(self):
        from fundos.research.adapters.website import _tables_to_text

        tables = _tables_to_text(self.HTML)
        self.assertEqual(len(tables), 1)
        self.assertIn("FY24 | 7,761 Cr", tables[0])
        # A merged cell is layout, not data.
        self.assertNotIn("Source: annual report", tables[0])

    def test_a_layout_table_is_ignored(self):
        from fundos.research.adapters.website import _tables_to_text

        self.assertEqual(
            _tables_to_text("<table><tr><td>just one row</td></tr></table>"),
            [])


class CircuitBreakerTests(TestCase):
    """One dead host must not burn the batch."""

    def setUp(self):
        from fundos.research.adapters import website

        website._BREAKER_FAILURES.clear()
        website._BREAKER_OPENED.clear()

    def test_the_circuit_opens_after_repeated_failures(self):
        from fundos.research.adapters import website

        for _ in range(website._BREAKER_THRESHOLD):
            website._breaker_record("dead.example.com", False)
        self.assertTrue(website._breaker_is_open("dead.example.com"))
        self.assertFalse(website._breaker_is_open("fine.example.com"))

    def test_success_closes_it(self):
        from fundos.research.adapters import website

        website._breaker_record("flaky.example.com", False)
        website._breaker_record("flaky.example.com", True)
        self.assertFalse(website._breaker_is_open("flaky.example.com"))
        self.assertEqual(website._BREAKER_FAILURES["flaky.example.com"], 0)
