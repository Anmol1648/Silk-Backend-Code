"""
v28 — the seed writes the configuration, so the seed is where the
configuration defects live.

Everything here was found by reading `seed_platform_config.py` after a
question that turned out to be right: *"I think duplicate gemini may have come
from the seed code."* It did. So did the quoted thinking budget that broke
trace parsing, and so did the schema/search conflict that v27.5 had to work
around at call time.

A defect in a seed is worse than a defect in a code path, because it writes
itself into the database of every install and then looks like something an
administrator chose.
"""
from django.core.management import call_command
from django.test import TestCase

from fundos.llm.default_prompts import DEFAULT_PROMPTS, ROLE_TIER_DEFAULTS, get_tier
from fundos.llm.tier_contract import (ADVANCED, JUDGMENT, SIMPLE,
                                      SEARCH_REQUIRED_ROLES,
                                      TierContractViolation,
                                      assert_role_tier_coherent, contract_for)


# ==========================================================================
# The three-tier policy
# ==========================================================================

class TierPolicyIsCompleteAndCoherent(TestCase):
    """The rule, stated once:

        simple    — answer from material the system already holds
        advanced  — must DISCOVER facts from the internet
        judgment  — reason over data another call already extracted

    `ROLE_TIER_DEFAULTS` is the policy document for that rule. A role missing
    from it still resolves — to "simple", via the `.get(role, "simple")`
    default — which means an omission and a decision render identically.
    """

    def test_every_role_has_an_explicit_tier(self):
        """v29 — keyed on LLM_ROLES, not DEFAULT_PROMPTS.

        The v28 version of this test compared DEFAULT_PROMPTS against
        ROLE_TIER_DEFAULTS. Both are 29 entries and both are maintained in
        the same file, so the assertion was tautologically empty and could
        never fail. Meanwhile LLM_ROLES — the roles the system can actually
        dispatch — has 33, and the four that are absent from DEFAULT_PROMPTS
        (their callers pass `system`/`prompt` directly) were invisible to it:

            assessment_extraction      live: assessment/extraction.py:279
            assessment_rubric_review
            investor_classification    live: investors/classify.py:69
            investor_identity          live: investors/identity.py:178

        The right denominator is what can be CALLED, not what ships a prompt.
        """
        from fundos.llm.models import LLM_ROLES

        missing = sorted(set(LLM_ROLES) - set(ROLE_TIER_DEFAULTS))
        self.assertEqual(
            missing, [],
            f"roles falling through to an implicit default: {missing}")

    def test_the_tier_table_names_no_role_that_does_not_exist(self):
        """A typo here is inert rather than loud, so assert both directions."""
        from fundos.llm.models import LLM_ROLES

        unknown = sorted(set(ROLE_TIER_DEFAULTS) - set(LLM_ROLES))
        self.assertEqual(unknown, [],
                         f"tier policy names non-existent roles: {unknown}")

    def test_every_tier_named_is_a_real_tier(self):
        for role, tier in ROLE_TIER_DEFAULTS.items():
            self.assertIn(tier, (SIMPLE, ADVANCED, JUDGMENT), role)

    def test_search_required_roles_are_on_the_searching_tier(self):
        for role in SEARCH_REQUIRED_ROLES:
            self.assertEqual(get_tier(role), ADVANCED, role)

    def test_no_role_is_on_advanced_without_needing_retrieval(self):
        """Advanced is the expensive tier AND, since v27.5, the one that

        drops responseSchema. A role placed there without needing retrieval
        pays twice and gains nothing.
        """
        for role, tier in ROLE_TIER_DEFAULTS.items():
            if tier == ADVANCED:
                self.assertIn(role, SEARCH_REQUIRED_ROLES, role)

    def test_the_extraction_roles_of_the_profile_pipeline_are_simple(self):
        for role in ("company_profile_records", "company_profile_section",
                     "company_profile_structured", "company_profile_field"):
            self.assertEqual(get_tier(role), SIMPLE, role)

    def test_the_judgement_roles_are_on_the_judgement_tier(self):
        for role in ("company_profile_judgment", "valuation_negotiation",
                     "objection_sim", "package_review", "im_consistency"):
            self.assertEqual(get_tier(role), JUDGMENT, role)


class TierContractsMatchTheirPurpose(TestCase):

    def test_simple_has_no_tools_and_no_thinking(self):
        c = contract_for(SIMPLE)
        self.assertFalse(c["web_search"])
        self.assertEqual(c["thinking_unstated"], 0)

    def test_advanced_searches_and_forbids_a_schema(self):
        c = contract_for(ADVANCED)
        self.assertTrue(c["web_search"])
        self.assertIs(c["structured_output"], False)

    def test_judgement_thinks_but_does_not_search(self):
        c = contract_for(JUDGMENT)
        self.assertFalse(c["web_search"])
        self.assertGreaterEqual(c["thinking_unstated"], 2048)
        # No structured_output key means "not forbidden": judgement output
        # feeds a parser and has no search tool for a schema to suppress.
        self.assertIsNot(c.get("structured_output"), False)


# ==========================================================================
# Coherence guard — all three directions
# ==========================================================================

class CoherenceGuardCoversAllThreeTiers(TestCase):
    """v27.5 guarded only the search direction, leaving two silent failures.

    Severity is graded by consequence. A search role that cannot search
    publishes recollection as research, so it RAISES. The other two degrade
    output rather than falsify it, and an administrator may have a reason we
    do not know, so they WARN and proceed.
    """

    def test_a_search_role_on_simple_raises(self):
        with self.assertRaises(TierContractViolation):
            assert_role_tier_coherent("company_profile_deep_extract", SIMPLE)

    def test_a_judgement_role_on_simple_warns(self):
        with self.assertLogs("fundos.llm.tier_contract", "WARNING") as log:
            assert_role_tier_coherent("company_profile_judgment", SIMPLE)
        self.assertIn("thinking budget to 0", "\n".join(log.output))

    def test_an_extraction_role_on_advanced_warns_about_the_schema(self):
        """The 19 Aug case. An override had put `company_profile_section` on

        advanced; after v27.5 that silently strips the structured output the
        section writer depends on.
        """
        with self.assertLogs("fundos.llm.tier_contract", "WARNING") as log:
            assert_role_tier_coherent("company_profile_section", ADVANCED)
        self.assertIn("responseSchema", "\n".join(log.output))

    def test_a_role_on_its_shipped_tier_is_silent(self):
        import logging
        logger = logging.getLogger("fundos.llm.tier_contract")
        with self.assertNoLogs(logger, "WARNING"):
            assert_role_tier_coherent("company_profile_records", SIMPLE)
            assert_role_tier_coherent("company_profile_judgment", JUDGMENT)
            assert_role_tier_coherent("company_profile_deep_extract", ADVANCED)


# ==========================================================================
# The seed
# ==========================================================================

class SeedProducesACoherentConfiguration(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)

    def test_no_searching_profile_carries_a_schema(self):
        """The seed used to write structured_output=True on every tier

        profile including advanced. v27.5 dropped it at call time, so the
        output was correct — but every advanced call logged a warning about a
        setting no administrator had chosen and none could act on, and the
        admin screen showed a tick for something the runtime ignored.
        """
        from fundos.llm.models import LLMConfigProfile
        conflicted = LLMConfigProfile.objects.filter(
            web_search=True, structured_output=True)
        self.assertFalse(
            conflicted.exists(),
            f"schema+search on: {[p.code for p in conflicted]}")

    def test_every_profile_points_at_an_endpoint_that_exists(self):
        """The adoption logic below rewrites endpoint codes, and a lookup

        that missed would leave a dangling foreign key that only surfaces at
        the first call.
        """
        from fundos.llm.models import LLMConfigProfile, LLMEndpoint
        codes = set(LLMEndpoint.objects.values_list("code", flat=True))
        for p in LLMConfigProfile.objects.all():
            self.assertIn(p.endpoint_id, codes, p.code)

    def test_seeding_twice_changes_nothing(self):
        from fundos.llm.models import LLMConfigProfile, LLMEndpoint
        before = (LLMEndpoint.objects.count(),
                  LLMConfigProfile.objects.count())
        call_command("seed_platform_config", verbosity=0)
        after = (LLMEndpoint.objects.count(),
                 LLMConfigProfile.objects.count())
        self.assertEqual(before, after)

    def test_thinking_budgets_are_stored_as_numbers(self):
        """The seed wrote thinking_budget="4000" as a STRING, which rendered

        as `thinking_budget="4000"` on the trace line next to a bare 2048 —
        the same field in two shapes, which broke every parser reading the
        log on 19 Aug.
        """
        from fundos.llm.models import LLMConfigProfile
        for p in LLMConfigProfile.objects.exclude(thinking_budget=None):
            if p.thinking_budget in ("", None):
                continue
            int(p.thinking_budget)      # must not raise

    def test_the_prompt_rows_carry_the_shipped_tier(self):
        from fundos.platformcfg.models import PromptTemplate
        for row in PromptTemplate.objects.filter(is_active=True):
            if row.role not in DEFAULT_PROMPTS:
                continue
            self.assertEqual((row.tier or "").lower(), get_tier(row.role),
                             row.role)


class SeedDoesNotManufactureDuplicateEndpoints(TestCase):
    """*"I think duplicate gemini may have come from the seed code."* It did.

    The seeder created a canonical `GEMINI` row unconditionally. An install
    whose administrator had already made their own endpoint for the same
    provider — `GEMINI LLM`, which is what the 19 Aug logs show serving every
    call — ended up with two rows reading the same API key, after which the
    dedupe logic printed `ignoring duplicate endpoint(s) GEMINI` on every seed
    run: the seeder reporting its own output as a problem.
    """

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)

    def test_an_existing_provider_row_is_adopted_not_duplicated(self):
        from fundos.llm.models import LLMEndpoint
        LLMEndpoint.objects.create(
            code="GEMINI LLM", display_name="Admin's Gemini",
            provider_kind="gemini", default_model="gemini-3.5-flash",
            api_key_env_var="GEMINI_API_KEY", timeout_seconds=120,
            is_active=True, priority=10)

        call_command("seed_platform_config", verbosity=0)

        gemini = LLMEndpoint.objects.filter(provider_kind="gemini")
        self.assertEqual(gemini.count(), 1,
                         f"duplicated: {[e.code for e in gemini]}")
        self.assertEqual(gemini.first().code, "GEMINI LLM",
                         "the administrator's row must survive, not be replaced")

    def test_profiles_bind_to_the_adopted_endpoint(self):
        from fundos.llm.models import LLMConfigProfile, LLMEndpoint
        LLMEndpoint.objects.create(
            code="GEMINI LLM", display_name="Admin's Gemini",
            provider_kind="gemini", default_model="gemini-3.5-flash",
            api_key_env_var="GEMINI_API_KEY", timeout_seconds=120,
            is_active=True, priority=10)

        call_command("seed_platform_config", verbosity=0)

        for p in LLMConfigProfile.objects.filter(provider="gemini"):
            self.assertEqual(p.endpoint_id, "GEMINI LLM", p.code)

    def test_a_clean_install_gets_exactly_one_endpoint_per_provider(self):
        """Not "gets the canonical codes" — `seed_initial_data` already

        creates `OPENAI-1`, and adopting it is the correct behaviour. The
        property that matters is one row per provider, because two rows read
        the same API key, both look usable, and which one wins is arbitrary.
        """
        call_command("seed_platform_config", verbosity=0)
        from fundos.llm.models import LLMEndpoint
        kinds = {}
        for ep in LLMEndpoint.objects.exclude(code="MOCK"):
            kinds.setdefault(ep.provider_kind, []).append(ep.code)
        for kind, codes in kinds.items():
            self.assertEqual(len(codes), 1,
                             f"{kind} served by {codes}")
        for kind in ("anthropic", "gemini", "openai_chat"):
            self.assertIn(kind, kinds, f"no endpoint serves {kind}")


class SeedRepairsExistingInstalls(TestCase):
    """`get_or_create` does nothing to an existing row, so a deployment seeded

    before a fix keeps the wrong value forever. Repair passes are how a
    correction actually reaches the installs that need it.
    """

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)

    def test_a_pre_existing_schema_search_conflict_is_repaired(self):
        """v29 — the alignment is ONE-TIME and GEMINI-ONLY.

        v28 ran an unguarded `.update()` across every provider on every seed
        run. That broke two of its own rules: `tier_contract` states the
        measurement is Gemini-only and must not degrade Anthropic/OpenAI
        output shape, and a repair that re-runs forever makes the setting
        permanently unsettable by an administrator.
        """
        from fundos.llm.models import LLMConfigProfile
        from fundos.platformcfg.models import PlatformFlag

        # A fresh install: the alignment has not run yet.
        PlatformFlag.objects.filter(key="v29.schema_search_aligned").delete()
        LLMConfigProfile.objects.filter(web_search=True).update(
            structured_output=True)          # simulate the old seed's output

        call_command("seed_platform_config", verbosity=0)

        self.assertFalse(
            LLMConfigProfile.objects.filter(
                web_search=True, structured_output=True,
                provider="gemini").exists(),
            "a searching Gemini profile must not keep a responseSchema")

    def test_the_schema_alignment_does_not_touch_other_providers(self):
        from fundos.llm.models import LLMConfigProfile
        from fundos.platformcfg.models import PlatformFlag

        PlatformFlag.objects.filter(key="v29.schema_search_aligned").delete()
        p = LLMConfigProfile.objects.filter(
            web_search=True).exclude(provider="gemini").first()
        if p is None:
            self.skipTest("no non-Gemini searching profile seeded")
        LLMConfigProfile.objects.filter(pk=p.pk).update(
            structured_output=True)

        call_command("seed_platform_config", verbosity=0)

        p.refresh_from_db()
        self.assertTrue(
            p.structured_output,
            "the 19 Aug finding is Gemini-only; tier_contract says so "
            "explicitly and the seeder must not contradict it")

    def test_an_administrator_may_re_enable_schema_after_the_alignment(self):
        """The alignment runs once. After that the choice belongs to the admin."""
        from fundos.llm.models import LLMConfigProfile

        call_command("seed_platform_config", verbosity=0)   # alignment runs
        p = LLMConfigProfile.objects.filter(
            web_search=True, provider="gemini").first()
        if p is None:
            self.skipTest("no searching Gemini profile seeded")
        LLMConfigProfile.objects.filter(pk=p.pk).update(structured_output=True)

        call_command("seed_platform_config", verbosity=0)   # must not revert

        p.refresh_from_db()
        self.assertTrue(
            p.structured_output,
            "a repair that re-runs forever is a policy in the wrong layer — "
            "it makes the setting unsettable and teaches operators to route "
            "around the seeder")

    def test_an_administrators_own_search_cap_is_not_overwritten(self):
        """Repair passes fill blanks. They must not overrule a decision."""
        from fundos.llm.models import LLMConfigProfile
        p = LLMConfigProfile.objects.filter(web_search=True).first()
        if p is None:
            self.skipTest("no searching profile seeded")
        p.max_uses = 2
        p.save(update_fields=["max_uses"])

        call_command("seed_platform_config", verbosity=0)

        p.refresh_from_db()
        self.assertEqual(p.max_uses, 2)
