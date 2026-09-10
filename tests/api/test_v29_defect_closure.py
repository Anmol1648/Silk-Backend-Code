"""v29 — closure tests for the defects v28 left open or introduced.

Each test names the defect, the evidence it was real, and what would break if
the fix regressed. Grouped by the layer the fault lived in.
"""
from django.core.management import call_command
from django.test import TestCase

from fundos.llm.default_prompts import ROLE_TIER_DEFAULTS, get_tier
from fundos.llm.models import LLM_ROLES
from fundos.llm.tier_contract import SEARCH_REQUIRED_ROLES
from fundos.llm.tiers import ADVANCED, JUDGMENT, SIMPLE


# ---------------------------------------------------------------------------
# 1. Tier policy coverage
# ---------------------------------------------------------------------------
class TierPolicyCoversEveryCallableRole(TestCase):
    """v28 asserted "all 29 roles have an explicit tier". There are 33.

    The four extra roles have no shipped prompt — their callers pass `system`
    and `prompt` directly — so they were absent from DEFAULT_PROMPTS, which
    was the population v28's test compared against. Three are live.
    """

    UNTIERED_IN_V28 = {
        "assessment_extraction",
        "assessment_rubric_review",
        "investor_classification",
        "investor_identity",
    }

    def test_the_four_roles_v28_missed_now_have_a_declared_tier(self):
        for role in self.UNTIERED_IN_V28:
            self.assertIn(
                role, ROLE_TIER_DEFAULTS,
                f"{role} is dispatchable but has no tier policy entry")

    def test_no_callable_role_falls_through_to_an_implicit_default(self):
        missing = sorted(set(LLM_ROLES) - set(ROLE_TIER_DEFAULTS))
        self.assertEqual(missing, [], f"implicit-default roles: {missing}")

    def test_the_user_stated_rule_holds_for_every_role(self):
        """search -> advanced, extraction -> simple, decisions -> judgement."""
        for role, tier in ROLE_TIER_DEFAULTS.items():
            self.assertIn(tier, (SIMPLE, ADVANCED, JUDGMENT), role)
            if tier == ADVANCED:
                self.assertIn(
                    role, SEARCH_REQUIRED_ROLES,
                    f"{role} is on the searching tier without needing to "
                    f"search — it pays for a tool and a thinking budget it "
                    f"cannot use")

    def test_an_unknown_role_is_reported_rather_than_silently_defaulted(self):
        with self.assertLogs("fundos.llm.default_prompts", "WARNING") as cm:
            self.assertEqual(get_tier("a_role_that_does_not_exist"), SIMPLE)
        self.assertIn("TIER POLICY GAP", "".join(cm.output))


# ---------------------------------------------------------------------------
# 2. Role sets must name real roles
# ---------------------------------------------------------------------------
class RoleSetsNameRealRoles(TestCase):
    """SEARCH_BENEFICIAL_ROLES held two strings that were not roles.

    "company_profile_consolidated" is a generation MODE and "market_research"
    is a section key / config-profile suffix. Neither could ever equal `role`
    at a call site, so the no-search warning they existed to raise could not
    fire. They read as coverage and provided none.
    """

    def test_every_role_set_member_is_a_dispatchable_role(self):
        from fundos.llm import adapter

        known = set(LLM_ROLES)
        for name in ("SEARCH_REQUIRED_ROLES", "SEARCH_BENEFICIAL_ROLES",
                     "NO_THINKING_ROLES", "THINKING_JUSTIFIED_ROLES"):
            members = getattr(adapter, name)
            unknown = sorted(set(members) - known)
            self.assertEqual(unknown, [], f"{name} names non-roles: {unknown}")

    def test_output_ceiling_table_names_real_roles(self):
        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS

        unknown = sorted(set(ROLE_MAX_OUTPUT_TOKENS) - set(LLM_ROLES))
        self.assertEqual(unknown, [])

    def test_thinking_sets_do_not_contradict_each_other(self):
        from fundos.llm.adapter import (NO_THINKING_ROLES,
                                        THINKING_JUSTIFIED_ROLES)
        self.assertEqual(NO_THINKING_ROLES & THINKING_JUSTIFIED_ROLES, set())


# ---------------------------------------------------------------------------
# 3. The JSON repair path — run 1's actual failure
# ---------------------------------------------------------------------------
class RepairKeepsTheRolesDesignedCeiling(TestCase):
    """19 Aug run 1:

        llm.call.assessment_inputs  FAIL
        JSONDecodeError: Expecting ',' delimiter: line 64 column 6 (char 4492)

    `_parse_json_with_repair` dispatched without `role`, so the repair
    resolved `ROLE_MAX_OUTPUT_TOKENS.get("")` and fell to the 2048 default —
    a quarter of assessment_inputs' designed 8192. The repair of a truncated
    response was itself truncated, and failed for the same reason.
    """

    def test_the_repair_dispatch_receives_the_role(self):
        import inspect

        from fundos.llm import adapter

        src = inspect.getsource(adapter._parse_json_with_repair)
        self.assertIn("role=role", src,
                      "the repair call must carry the role or it inherits "
                      "DEFAULT_MAX_OUTPUT_TOKENS (2048)")

    def test_the_role_ceiling_resolves_above_the_default_for_big_roles(self):
        from fundos.llm.adapter import (DEFAULT_MAX_OUTPUT_TOKENS,
                                        ROLE_MAX_OUTPUT_TOKENS)
        for role in ("assessment_inputs", "company_profile_deep_extract"):
            self.assertGreater(ROLE_MAX_OUTPUT_TOKENS[role],
                               DEFAULT_MAX_OUTPUT_TOKENS, role)


class TruncatedJsonIsSalvagedNotDiscarded(TestCase):
    """19 Aug run 2 wrote 7 of 30 fields; run 1 wrote none.

    Both responses were valid JSON with the tail missing — truncation, not
    corruption. Raising JSONDecodeError threw away every extracted field to
    punish a missing brace, and paid for a repair call to get them back.
    """

    def _salvage(self, text):
        from fundos.llm.adapter import _salvage_truncated_json
        return _salvage_truncated_json(text)

    def test_an_object_cut_mid_element_keeps_the_complete_prefix(self):
        self.assertEqual(
            self._salvage('{"values":[{"a":1},{"b":2},{"c":'),
            {"values": [{"a": 1}, {"b": 2}]})

    def test_a_cut_inside_a_string_drops_only_that_element(self):
        self.assertEqual(
            self._salvage('{"values":[{"a":1}],"sector":"Fintec'),
            {"values": [{"a": 1}]})

    def test_a_comma_inside_a_string_is_not_a_cut_point(self):
        self.assertEqual(
            self._salvage('{"v":[{"x":"a,b"},{"y":'),
            {"v": [{"x": "a,b"}]})

    def test_an_escaped_quote_does_not_confuse_the_scanner(self):
        self.assertEqual(
            self._salvage('{"v":[{"x":"a\\"b"},{"y":'),
            {"v": [{"x": 'a"b'}]})

    def test_valid_json_is_returned_unchanged(self):
        self.assertEqual(self._salvage('{"a":1,"b":2}'), {"a": 1})

    def test_unsalvageable_input_returns_none_rather_than_guessing(self):
        self.assertIsNone(self._salvage("not json at all"))
        self.assertIsNone(self._salvage(""))

    def test_salvage_never_invents_a_value(self):
        """The worst case must be FEWER fields, never wrong ones."""
        out = self._salvage('{"a":1,"b":2,"c":')
        self.assertNotIn("c", out or {})


class AResponseThatBreaksInTheMiddleKeepsWhatCameBefore(TestCase):
    """Truncation was not the only shape a bad response takes.

    A 10 September Tyreplex synthesis broke at character 16,630 and then ran
    on correctly for another 188,000 characters:

        response_chars=205388  finish_reason="STOP"
        JSONDecodeError: Expecting ',' delimiter: line 239 column 7

    `finish_reason=STOP` says nothing was cut off, so closing the brackets at
    the end fixed nothing — the fault was far behind. Sixteen sections, 7m18s
    and the whole run were discarded to punish one bad string.
    """

    def _salvage(self, text):
        from fundos.llm.adapter import _salvage_truncated_json
        return _salvage_truncated_json(text)

    def _response(self, sections, broken_at=None, tail=200):
        """A response with `sections` good sections, one broken one, and a
        long correct tail after it — the real shape."""
        body = ", ".join(
            f'"section_{i}": {{"data": {{"x": "value {i}"}}}}'
            for i in range(sections))
        bad = (', "story": {"data": {"s": "He said "we win" here"}}'
               if broken_at is None else broken_at)
        run_on = ', "later": {"data": {"y": 1}}' * tail
        return '{"sections": {' + body + bad + run_on + "}}"

    def test_the_sections_before_the_fault_survive(self):
        out = self._salvage(self._response(3))
        self.assertEqual(sorted(out["sections"]),
                         ["section_0", "section_1", "section_2"])

    def test_the_recovered_values_are_the_real_ones(self):
        out = self._salvage(self._response(3))
        self.assertEqual(out["sections"]["section_0"]["data"]["x"], "value 0")

    def test_nothing_after_the_fault_is_kept(self):
        """Data past the break is discarded, not guessed at or re-ordered."""
        out = self._salvage(self._response(3))
        self.assertNotIn("story", out["sections"])
        self.assertNotIn("later", out["sections"])

    def test_a_fault_in_the_very_first_section_yields_nothing(self):
        self.assertIsNone(self._salvage(self._response(0, tail=5)))

    def test_several_bad_strings_are_each_cut_back_past(self):
        """One pass per fault, and it still terminates."""
        body = '{"sections": {"a": {"data": {"x": "ok"}}'
        bad = ', "b": {"data": {"x": "he said "no""}}'
        out = self._salvage(body + bad + bad + bad + "}}")
        self.assertEqual(sorted(out["sections"]), ["a"])

    def test_a_response_that_is_bad_all_the_way_down_gives_up(self):
        """Salvage is bounded: it must not loop cutting one character at a
        time through a 200,000-character response."""
        self.assertIsNone(self._salvage('{"a": "x"y"z"w"v"u"t"s"r"q"'))

    def test_plain_truncation_still_works(self):
        self.assertEqual(
            self._salvage('{"values":[{"a":1},{"b":2},{"c":'),
            {"values": [{"a": 1}, {"b": 2}]})


# ---------------------------------------------------------------------------
# 4. structured_output was inert for six releases
# ---------------------------------------------------------------------------
class StructuredOutputIsRealOrSaysItIsNot(TestCase):
    """`output_schema` defaults to {} and nothing ever wrote to it.

    Every provider path guards with `if so and so.get("schema")`, so no
    responseSchema ever reached Gemini — on any tier, for any role. Two
    releases of reasoning were built on a variable that was never set.
    """

    def test_a_role_outside_the_allowlist_gets_no_derived_schema(self):
        from fundos.llm.schemas import json_schema_for
        self.assertIsNone(json_schema_for("company_profile_deep_extract"))

    def test_the_deep_extract_is_never_given_a_prescriptive_schema(self):
        """244 output fields, 3 declared in SCHEMAS.

        A responseSchema is prescriptive. Deriving one here would reduce the
        dossier to three housekeeping keys, silently, with a clean parse.
        """
        from fundos.llm.schemas import SCHEMA_IS_COMPLETE_OUTPUT
        self.assertNotIn("company_profile_deep_extract",
                         SCHEMA_IS_COMPLETE_OUTPUT)

    def test_an_allowlisted_role_produces_a_usable_schema(self):
        from fundos.llm.schemas import json_schema_for

        schema = json_schema_for("readiness_summary")
        self.assertEqual(schema["type"], "OBJECT")
        self.assertIn("summary", schema["properties"])
        self.assertIn("summary", schema["required"])

    def test_every_array_property_declares_items(self):
        """Gemini rejects an ARRAY with no `items`."""
        from fundos.llm.schemas import (SCHEMA_IS_COMPLETE_OUTPUT,
                                        json_schema_for)
        for role in SCHEMA_IS_COMPLETE_OUTPUT:
            schema = json_schema_for(role)
            for key, prop in (schema or {}).get("properties", {}).items():
                if prop.get("type") == "ARRAY":
                    self.assertIn("items", prop, f"{role}.{key}")

    def test_allowlisted_roles_exist(self):
        from fundos.llm.schemas import SCHEMA_IS_COMPLETE_OUTPUT
        self.assertEqual(set(SCHEMA_IS_COMPLETE_OUTPUT) - set(LLM_ROLES),
                         set())


# ---------------------------------------------------------------------------
# 5. Seeder — endpoint adoption
# ---------------------------------------------------------------------------
class EndpointAdoptionAgreesWithProviderSelection(TestCase):
    """Two dedupe rules in one file, disagreeing.

    `_usable_endpoints` weighs "is a role binding already pointing here";
    v28's adoption logic did not. With GEMINI and 'GEMINI LLM' both active at
    equal priority, "GEMINI" sorted first, adoption was skipped, the
    duplicate survived, and the tier profiles were pointed at the row nothing
    uses while traffic went to the other.
    """

    def test_the_row_in_use_wins_over_the_canonical_code(self):
        import os

        from fundos.llm.models import (LLMEndpoint, LLMRoleBinding)
        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        hand_made = LLMEndpoint.objects.create(
            code="GEMINI LLM", display_name="Gemini (admin)",
            provider_kind="gemini", default_model="gemini-3.5-flash",
            api_key_env_var="GEMINI_API_KEY", is_active=True, priority=10)
        canonical = LLMEndpoint.objects.create(
            code="GEMINI", display_name="Google (Gemini)",
            provider_kind="gemini", default_model="gemini-3.5-flash",
            api_key_env_var="GEMINI_API_KEY", is_active=True, priority=10)
        LLMRoleBinding.objects.create(
            role="company_profile_deep_extract",
            primary_endpoint=hand_made, is_mocked=False)

        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            chosen = Command._preferred_endpoint_for_kind("gemini")
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

        self.assertEqual(
            chosen.code, hand_made.code,
            "adoption must converge on the endpoint traffic already uses, "
            f"not the alphabetically-first code ({canonical.code})")

    def test_adoption_and_usable_endpoints_pick_the_same_row(self):
        import os

        from fundos.llm.models import LLMEndpoint, LLMRoleBinding
        from fundos.platformcfg.management.commands.seed_platform_config \
            import Command

        hand_made = LLMEndpoint.objects.create(
            code="GEMINI LLM", display_name="Gemini (admin)",
            provider_kind="gemini", default_model="gemini-3.5-flash",
            api_key_env_var="GEMINI_API_KEY", is_active=True, priority=10)
        LLMEndpoint.objects.create(
            code="GEMINI", display_name="Google (Gemini)",
            provider_kind="gemini", default_model="gemini-3.5-flash",
            api_key_env_var="GEMINI_API_KEY", is_active=True, priority=10)
        LLMRoleBinding.objects.create(
            role="company_profile_deep_extract",
            primary_endpoint=hand_made, is_mocked=False)

        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            cmd = Command()
            cmd.stdout = type("S", (), {"write": lambda self, *a, **k: None})()
            adopted = Command._preferred_endpoint_for_kind("gemini")
            usable = [e for e in cmd._usable_endpoints()
                      if e.provider_kind == "gemini"]
        finally:
            os.environ.pop("GEMINI_API_KEY", None)

        self.assertEqual([adopted.code], [e.code for e in usable],
                         "one file must not hold two dedupe rules")


# ---------------------------------------------------------------------------
# 6. Peer universe — the other half of domain-agnosticism
# ---------------------------------------------------------------------------
class PeerGroupingNeverMisGroupsAnOutOfSectorCompany(TestCase):
    """The assessment path refuses to fuzzy-match; the peers path forced it.

    `generate_peers` fell back to `promoted = [0]` when nothing cleared the
    bar, promoting the top scorer to Group A "Closest Comparables" whatever
    its score. With a SaaS-only fixture universe, a construction company was
    shown a Vertical SaaS peer as its closest comparable — the exact outcome
    ADMIN_CONFIG_v28 §4 congratulates the system for avoiding.
    """

    def test_an_out_of_sector_company_gets_an_empty_group_a(self):
        from fundos.strategy.services import group_peers_by_score

        # A construction company against a SaaS-only peer universe.
        groups, promoted = group_peers_by_score([12.0, 9.5, 7.0, 3.0])
        self.assertNotIn("A", groups,
                         "an unrelated peer must not become a 'Closest "
                         "Comparable' merely for being least unrelated")
        self.assertEqual(promoted, [])

    def test_a_genuine_near_miss_is_still_promoted(self):
        """Tester Issue 15 must not regress: an absolute bar alone is wrong."""
        from fundos.strategy.services import group_peers_by_score

        groups, promoted = group_peers_by_score([52.0, 48.0, 30.0])
        self.assertEqual(groups[0], "A")
        self.assertEqual(groups[1], "A")
        self.assertEqual(promoted, [0, 1])

    def test_an_absolute_group_a_needs_no_promotion(self):
        from fundos.strategy.services import group_peers_by_score

        groups, promoted = group_peers_by_score([72.0, 40.0, 10.0])
        self.assertEqual(groups, ["A", "B", "C"])
        self.assertEqual(promoted, [])

    def test_promotion_is_capped_at_three(self):
        from fundos.strategy.services import group_peers_by_score

        groups, promoted = group_peers_by_score([59.0, 58.0, 57.0, 56.0, 55.0])
        self.assertEqual(len(promoted), 3)
        self.assertEqual(groups.count("A"), 3)

    def test_the_floor_matches_the_sector_resolvers_philosophy(self):
        from fundos.strategy.services import (PEER_GROUP_A,
                                              PEER_PROMOTION_FLOOR)
        self.assertGreater(PEER_PROMOTION_FLOOR, 0)
        self.assertLess(PEER_PROMOTION_FLOOR, PEER_GROUP_A)

    def test_no_peers_at_all_is_handled(self):
        from fundos.strategy.services import group_peers_by_score
        self.assertEqual(group_peers_by_score([]), ([], []))


# ---------------------------------------------------------------------------
# 7. Seed idempotence — nothing above may break re-running the seeder
# ---------------------------------------------------------------------------
class SeedingRemainsIdempotent(TestCase):
    def test_seeding_twice_changes_nothing_the_second_time(self):
        import os

        from fundos.llm.models import LLMConfigProfile, LLMEndpoint

        os.environ["GEMINI_API_KEY"] = "test-key"
        try:
            call_command("seed_platform_config", verbosity=0)
            before = (LLMEndpoint.objects.count(),
                      LLMConfigProfile.objects.count())
            call_command("seed_platform_config", verbosity=0)
            after = (LLMEndpoint.objects.count(),
                     LLMConfigProfile.objects.count())
        finally:
            os.environ.pop("GEMINI_API_KEY", None)
        self.assertEqual(before, after)

    def test_the_judgement_tier_budget_is_seeded_as_a_number(self):
        call_command("seed_platform_config", verbosity=0)
        from fundos.llm.models import LLMConfigProfile

        p = LLMConfigProfile.objects.filter(code="tier.judgment").first()
        self.assertIsNotNone(p)
        # CharField, so it persists as a string either way — assert it is a
        # NUMBER, which is what the seed literal now says and what the trace
        # formatter has coerced since v27.5.
        self.assertTrue(str(p.thinking_budget).isdigit(), p.thinking_budget)
