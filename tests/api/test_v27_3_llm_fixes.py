"""v27.3 — tier contract and the LLM fixes that depend on it.

These tests assert on OUTCOMES, not on flags. v27 shipped a grounding gate
that fired correctly and still made the product worse, because the test
verified that the gate fired rather than what the user ended up with. So:
what capabilities reach the wire, what a run costs, what gets written.
"""
from decimal import Decimal

from django.core.exceptions import ValidationError
from django.test import SimpleTestCase, TestCase

from fundos.llm.tier_contract import (SEARCH_REQUIRED_ROLES,
                                      TierContractViolation, apply_contract,
                                      assert_role_tier_coherent)

GEMINI_CAPS = {"web_search", "url_context", "structured_output",
               "thinking_budget", "prompt_cache"}
NO_SEARCH_CAPS = {"structured_output"}


class TierContractTests(SimpleTestCase):
    """The tier decides; the profile may only tune within it."""

    def test_advanced_gets_search_even_when_profile_says_no(self):
        """The bug this whole release exists for.

        `tier="advanced", web_search=False` was a valid row that saved
        without complaint and generated from the model's memory.
        """
        caps = apply_contract({}, "advanced", provider="gemini",
                              provider_caps=GEMINI_CAPS)
        self.assertIn("web_search", caps)

    def test_advanced_gets_a_thinking_budget(self):
        """google_search is model-decided: permission without a deliberation
        step is permission the model cannot act on."""
        caps = apply_contract({}, "advanced", provider="gemini",
                              provider_caps=GEMINI_CAPS)
        self.assertGreater(caps["thinking"]["budget_tokens"], 0)

    def test_simple_never_searches_even_when_ticked(self):
        caps = apply_contract({"web_search": {"max_uses": 6}}, "simple",
                              provider="gemini", provider_caps=GEMINI_CAPS)
        self.assertNotIn("web_search", caps)

    def test_simple_keeps_thinking_off_on_gemini(self):
        """Gemini reasons by default and bills thoughts against
        maxOutputTokens. Silence means 'provider default', not 'off', so 0
        must still be stated explicitly on the highest-volume tier."""
        caps = apply_contract({}, "simple", provider="gemini",
                              provider_caps=GEMINI_CAPS)
        self.assertEqual(caps["thinking"]["budget_tokens"], 0)

    def test_judgment_reasons_but_cannot_browse(self):
        caps = apply_contract({"web_search": {}}, "judgment",
                              provider="gemini", provider_caps=GEMINI_CAPS)
        self.assertNotIn("web_search", caps)
        self.assertGreater(caps["thinking"]["budget_tokens"], 0)

    def test_profile_may_tune_the_budget_it_may_not_zero_it(self):
        tuned = apply_contract({"thinking": {"budget_tokens": "1024"}},
                               "advanced", provider="gemini",
                               provider_caps=GEMINI_CAPS)
        # Pass-through, not normalised: test_gemini_thinking already
        # pins the string form, and the adapter coerces at the wire.
        self.assertEqual(str(tuned["thinking"]["budget_tokens"]), "1024")

        # thinking_budget is a CharField, so "" reaches here and int("")
        # raises. That silently produced budget 0 — search off — before.
        blank = apply_contract({"thinking": {"budget_tokens": ""}},
                               "advanced", provider="gemini",
                               provider_caps=GEMINI_CAPS)
        self.assertGreater(blank["thinking"]["budget_tokens"], 0)

    def test_profile_keeps_control_of_max_uses(self):
        caps = apply_contract({"web_search": {"max_uses": 3}}, "advanced",
                              provider="gemini", provider_caps=GEMINI_CAPS)
        self.assertEqual(caps["web_search"]["max_uses"], 3)

    def test_advanced_on_a_provider_that_cannot_search_is_refused(self):
        """Silent degradation is the failure mode this module ends. A tier
        whose contract is retrieval must not quietly run without it."""
        with self.assertRaises(TierContractViolation):
            apply_contract({}, "advanced", provider="deepinfra",
                           provider_caps=NO_SEARCH_CAPS)


class RoleTierCoherenceTests(SimpleTestCase):

    def test_search_required_role_on_simple_tier_is_refused(self):
        with self.assertRaises(TierContractViolation):
            assert_role_tier_coherent("company_profile_deep_extract", "simple")

    def test_extraction_role_on_simple_tier_is_fine(self):
        assert_role_tier_coherent("company_profile_records", "simple")

    def test_every_search_required_role_has_a_shipped_advanced_default(self):
        """A role in SEARCH_REQUIRED_ROLES whose shipped tier is not advanced
        would raise on every call. Catch the mismatch here, not in prod."""
        from fundos.llm.default_prompts import get_tier
        for role in SEARCH_REQUIRED_ROLES:
            self.assertEqual(get_tier(role), "advanced", msg=role)


class UnweightedRevenueSplitTests(SimpleTestCase):
    """L-02 — 4 correct stream names were discarded on both 17 Aug runs."""

    def test_all_unweighted_rows_save_as_a_draft(self):
        from fundos.profile.records import validate_structured_form
        out = validate_structured_form("revenue_model", {"items": [
            {"label": "Subscription"}, {"label": "Transaction fees"},
            {"label": "Advertising"}, {"label": "Partnerships"}]})
        self.assertEqual(len(out["items"]), 4)
        self.assertTrue(out["unweighted"])

    def test_a_real_split_must_still_total_100(self):
        from fundos.profile.records import DomainValidationError
        from fundos.profile.records import validate_structured_form
        with self.assertRaises(DomainValidationError):
            validate_structured_form("revenue_model", {"items": [
                {"label": "Subscription", "pct": 30},
                {"label": "Transaction fees", "pct": 30}]})

    def test_a_partial_split_is_rejected_with_an_actionable_message(self):
        """Half-weighted rows are ambiguous — the missing ones could be 0%
        or unknown. Ask rather than guess."""
        from fundos.profile.records import DomainValidationError
        from fundos.profile.records import validate_structured_form
        with self.assertRaises(DomainValidationError):
            validate_structured_form("revenue_model", {"items": [
                {"label": "Subscription", "pct": 100},
                {"label": "Transaction fees"}]})

    def test_seeder_keeps_unweighted_rows_instead_of_dropping_them(self):
        from fundos.profile.services import _records_items_to_api
        rows, dropped = _records_items_to_api("revenue_model", [
            {"label": "Subscription"}, {"label": "Ads"}])
        self.assertEqual(len(rows), 2)
        self.assertNotIn("pct", rows[0])
        self.assertIn("draft", " ".join(dropped))


class ThinkingTokenCostTests(TestCase):
    """C-01 — thinking bills at the output rate and was never counted.

    Under-reported the 17 Aug runs by 39%. Matters more now: the v27.3
    contract turns thinking ON for two of the three tiers.
    """

    def setUp(self):
        from fundos.llm.models import LLMModelCost
        LLMModelCost.objects.create(
            provider="GEMINI LLM", model_name="gemini-3.5-flash",
            input_cost_per_1k_inr=Decimal("0.132"),
            output_cost_per_1k_inr=Decimal("0.792"),
            is_active=True)

    def test_thinking_tokens_are_billed_at_the_output_rate(self):
        from fundos.llm.models import LLMModelCost
        without = LLMModelCost.cost_inr(
            "GEMINI LLM", "gemini-3.5-flash", 1000, 1000)
        with_thinking = LLMModelCost.cost_inr(
            "GEMINI LLM", "gemini-3.5-flash", 1000, 1000 + 500)
        self.assertGreater(with_thinking, without)
        self.assertAlmostEqual(
            float(with_thinking - without),
            float(Decimal("0.792") * Decimal("0.5")), places=6)


class SearchReminderTests(SimpleTestCase):
    """L-01 / H3 — the directive sat 24,081 chars ahead of the task."""

    def test_reminder_is_short_enough_to_be_free(self):
        from fundos.llm.adapter import _search_reminder
        self.assertLess(len(_search_reminder()), 400)

    def test_reminder_contradicts_the_context_explicitly(self):
        """A model holding 24k chars of plausible material needs to be told
        that material is insufficient, not merely that search is allowed."""
        from fundos.llm.adapter import _search_reminder
        self.assertIn("NOT", _search_reminder())
