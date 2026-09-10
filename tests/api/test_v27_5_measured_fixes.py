"""
v27.5 — acting on what the 19 Aug production run measured.

Every test corresponds to something OBSERVED in
`silk_generation_19AUG26.log` or `LLM_LOGS_1.txt`, not to something reasoned
about. The docstrings quote the log line, because the whole failure mode of
this codebase has been fixing hypotheses instead of measurements.

Two of these tests exist to constrain a change I made in v27.4 that the data
subsequently falsified. That is the point of writing them down.
"""
from decimal import Decimal

from django.test import TestCase

from fundos.llm import adapter, ledger
from fundos.llm.tier_contract import ADVANCED, SIMPLE, TIER_CONTRACT, apply_contract


# ==========================================================================
# H1 — responseSchema suppresses google_search  (MEASURED 19 Aug)
# ==========================================================================

class SchemaSuppressesSearch(TestCase):
    """tools/diagnose_gemini_search.py, gemini-3.5-flash, 8 cells:

        schema=True  → searched 0 of 4
        schema=False → searched 4 of 4

    A responseSchema and google_search cannot coexist on this model. The
    advanced tier exists to retrieve facts that are not in the supplied
    context, so the schema is what gives way.
    """

    def test_the_advanced_tier_forbids_structured_output(self):
        self.assertIs(TIER_CONTRACT[ADVANCED]["structured_output"], False)

    def test_the_schema_is_dropped_on_gemini(self):
        caps = apply_contract(
            {"structured_output": {"schema": {"type": "object"}}},
            ADVANCED, provider="gemini",
            provider_caps={"web_search", "thinking_budget",
                           "structured_output"})
        self.assertNotIn("structured_output", caps)
        self.assertIn("web_search", caps,
                      "the tool this tier exists for must survive")

    def test_other_providers_are_untouched(self):
        """H1 was measured on Gemini. Anthropic and OpenAI have no equivalent

        interaction, and degrading their output shape on a Gemini finding
        would be applying a result beyond the evidence for it.
        """
        for provider in ("anthropic", "openai"):
            caps = apply_contract(
                {"structured_output": {"schema": {"type": "object"}}},
                ADVANCED, provider=provider,
                provider_caps={"web_search", "structured_output"})
            self.assertIn("structured_output", caps, provider)

    def test_the_simple_tier_keeps_its_schema(self):
        """A tier with no search tool has nothing to lose to the schema."""
        caps = apply_contract(
            {"structured_output": {"schema": {"type": "object"}}},
            SIMPLE, provider="gemini",
            provider_caps={"web_search", "structured_output"})
        self.assertIn("structured_output", caps)
        self.assertNotIn("web_search", caps)

    def test_the_measurement_is_recorded_not_just_applied(self):
        """The grid is written into the source with its date and model.

        Two halves of this repo asserted opposite answers to this question for
        six releases, both from reasoning. The result has to live somewhere it
        cannot be re-derived by argument.
        """
        import inspect
        from fundos.llm import tier_contract
        src = inspect.getsource(tier_contract)
        self.assertIn("MEASURED 19 Aug 2026", src)
        self.assertIn("gemini-3.5-flash", src)
        self.assertIn("diagnose_gemini_search", src)


# ==========================================================================
# H2 — thinking does NOT enable search. My v27.4 rule was wrong.
# ==========================================================================

class ThinkingIsNotASearchEnabler(TestCase):
    """v27.4 preserved the thinking budget on every search-required role, on

    the reasoning that a model-decided tool needs deliberation to be chosen.
    The 19 Aug diagnostic varied thinking against search directly and found
    NO EFFECT — without a schema the model searched at budget 0 (4 searches)
    as readily as at 2048 (3 and 6).

    What the rule did buy, in production: +5,243 to +8,809 thinking tokens per
    run, +56s latency, and a truncated `assessment_inputs` on both runs.
    """

    def test_the_exemption_is_no_longer_keyed_to_search(self):
        import inspect
        src = inspect.getsource(adapter._call_llm) if hasattr(
            adapter, "_call_llm") else inspect.getsource(adapter)
        self.assertIn("THINKING_JUSTIFIED_ROLES", src)
        self.assertNotIn('_needs_deliberation = (role in SEARCH_REQUIRED_ROLES',
                         src)

    def test_the_justified_set_is_small_and_reason_based(self):
        """Every role here costs output tokens on every run."""
        self.assertLessEqual(len(adapter.THINKING_JUSTIFIED_ROLES), 4)
        self.assertIn("company_profile_judgment",
                      adapter.THINKING_JUSTIFIED_ROLES)

    def test_extraction_roles_do_not_get_thinking_merely_for_having_a_tool(self):
        """`company_profile_section` is the exact 19 Aug case: an admin tier

        override put it on `advanced`, which handed it web_search, which under
        the v27.4 rule handed it 4,640 thinking tokens a run. It searched
        anyway when it searched at all, so the budget bought nothing.
        """
        role = "company_profile_section"
        self.assertIn(role, adapter.NO_THINKING_ROLES)
        self.assertNotIn(role, adapter.THINKING_JUSTIFIED_ROLES)

    def test_the_role_sets_still_cannot_conflict(self):
        self.assertEqual(
            adapter.THINKING_JUSTIFIED_ROLES & adapter.NO_THINKING_ROLES,
            set())


# ==========================================================================
# The output ceiling that killed assessment_inputs on both runs
# ==========================================================================

class AssessmentOutputCeiling(TestCase):
    """run 1: JSONDecodeError at char 4492 after 43.2s — assessment lost.

    run 2: finish_reason=MAX_TOKENS, thinking=1945, completion_tokens=0,
           was_repaired=true — 7 of 30 fields survived.

    The role had no entry in ROLE_MAX_OUTPUT_TOKENS and inherited the 2,048
    default, which does not hold 16-30 parameters with values, confidences
    and citations.
    """

    def test_assessment_inputs_has_an_explicit_ceiling(self):
        self.assertIn("assessment_inputs", adapter.ROLE_MAX_OUTPUT_TOKENS)

    def test_the_ceiling_clears_a_thinking_budget_with_room_to_spare(self):
        ceiling = adapter.ROLE_MAX_OUTPUT_TOKENS["assessment_inputs"]
        self.assertGreaterEqual(ceiling, 4096)
        # Observed thinking spend was 1,945 tokens. The answer needs the rest.
        self.assertGreater(ceiling - 1945, 4000)

    def test_it_is_not_left_on_the_default(self):
        self.assertNotEqual(
            adapter.ROLE_MAX_OUTPUT_TOKENS["assessment_inputs"],
            adapter.DEFAULT_MAX_OUTPUT_TOKENS)


# ==========================================================================
# Accounting: a billed failure is not a free one
# ==========================================================================

class FailedCallsAreCosted(TestCase):
    """`run.economics.role role="assessment_inputs" prompt_tokens=0

    cost_inr=0.0 latency_ms=43218` — 43 seconds of billed inference recorded
    as free. The provider generated every token; we simply could not parse
    the result.
    """

    def setUp(self):
        ledger.reset()

    def test_a_parse_failure_records_the_tokens_it_burned(self):
        adapter._log_call(
            role="assessment_inputs", endpoint_code="GEMINI",
            model="gemini-3.5-flash", deal_id=None, user=None,
            calling_context="t", latency_ms=43218, status="error",
            error="JSONDecodeError", prompt_tokens=3094,
            completion_tokens=1100, thinking_tokens=1945)
        entry = ledger.since(0)[0]
        self.assertEqual(entry["prompt_tokens"], 3094)
        self.assertEqual(entry["thinking_tokens"], 1945)
        self.assertEqual(entry["status"], "error")

    def test_a_pre_dispatch_failure_still_reports_honest_zeros(self):
        """DNS failures and timeouts generate nothing, so they cost nothing.

        The estimator must not invent spend for them.
        """
        adapter._log_call(
            role="company_profile_records", endpoint_code="GEMINI",
            model="m", deal_id=None, user=None, calling_context="t",
            latency_ms=30, status="error", error="ConnectionError",
            prompt_tokens=0, completion_tokens=0)
        entry = ledger.since(0)[0]
        self.assertEqual(entry["prompt_tokens"], 0)
        self.assertEqual(entry["completion_tokens"], 0)

    def test_usage_is_seeded_before_dispatch(self):
        """The regression guard. If `usage` is only bound by a successful

        `_dispatch`, the failure handler below it cannot see what was spent.
        """
        import inspect
        src = inspect.getsource(adapter)
        seed = src.index("usage = {}")
        dispatch = src.index("text, usage = _dispatch(")
        self.assertLess(seed, dispatch)


class TruncatedCallsReportTheirOutput(TestCase):
    """run 2: `completion_tokens=0 response_chars=485`. Gemini stops counting

    candidate tokens once a response is cut off at the ceiling, but it bills
    for what it produced.
    """

    def test_zero_with_text_is_estimated(self):
        n = adapter._completion_from({"completion_tokens": 0}, "x" * 485)
        self.assertGreater(n, 100)
        self.assertLess(n, 200)

    def test_a_genuine_empty_response_stays_zero(self):
        self.assertEqual(adapter._completion_from({"completion_tokens": 0}, ""), 0)

    def test_a_reported_count_is_never_overridden(self):
        """The provider's own number wins whenever it gives one."""
        self.assertEqual(
            adapter._completion_from({"completion_tokens": 7}, "x" * 4000), 7)


# ==========================================================================
# searches_unknown was too generous, and it understated the headline
# ==========================================================================

class UnknownSearchesResolveToZeroOnGemini(TestCase):
    """run 2 read `searches=6 searches_unknown=8`. The truth was six searches

    across three calls and none at all across the other eight.

    `company_profile_section` returned `webSearchQueries` on every call that
    searched, so the field is present whenever a search occurs. Its absence
    is therefore evidence of zero, not absence of evidence.
    """

    def test_absent_metadata_with_the_tool_attached_is_zero(self):
        self.assertEqual(
            adapter._count_searches("gemini", {}, tool_attached=True), 0)

    def test_absent_metadata_without_the_tool_stays_unknown(self):
        """Nothing was attached, so the model had nothing to decline. This is

        the case `searches_unknown` was built for and it must survive.
        """
        self.assertIsNone(
            adapter._count_searches("gemini", {}, tool_attached=False))

    def test_a_real_count_is_used_when_present(self):
        self.assertEqual(
            adapter._count_searches(
                "gemini", {"web_search_queries": ["a", "b", "c"]}), 3)

    def test_other_providers_are_unchanged(self):
        self.assertIsNone(
            adapter._count_searches("anthropic", {}, tool_attached=True))


# ==========================================================================
# The search_expected flag was measurably backwards
# ==========================================================================

class SearchInstructionFollowsTheTool(TestCase):
    """19 Aug: `search_expected=false` on `company_profile_section`, the only

    role in the system that searched (2 then 6 times), and `true` on the two
    that never did. An admin tier override had put section on `advanced` and
    handed it the tool without adding it to any role list — so it was paid for
    and never told about.
    """

    def test_the_directive_is_keyed_to_the_capability(self):
        import inspect
        src = inspect.getsource(adapter)
        self.assertIn("expect_search=True", src)
        self.assertNotIn("expect_search=role in SEARCH_EXPECTED_ROLES", src)

    def test_the_tail_reminder_is_keyed_to_the_capability(self):
        import inspect
        src = inspect.getsource(adapter)
        self.assertNotIn(
            'if role in SEARCH_EXPECTED_ROLES and "web_search" in capabilities:',
            src)

    def test_the_trace_reports_both_the_tool_and_the_declared_intent(self):
        """One boolean hid the disagreement. Two make it legible."""
        import inspect
        src = inspect.getsource(adapter)
        self.assertIn('search_expected=("web_search" in capabilities)', src)
        self.assertIn("role_expects_search=", src)


# ==========================================================================
# C-05 — the transaction abort that survived three releases
# ==========================================================================

class TransactionPoisoningIsClosed(TestCase):
    """`financials→CKB sync failed … You can't execute queries until the end

    of the 'atomic' block` has appeared on every run since v27.1.

    It is Django's TransactionManagementError, raised when
    `connection.needs_rollback` is set — which happens when a statement fails
    inside an atomic block and the exception is CAUGHT rather than allowed to
    unwind. The sync was never the fault; it was the next thing to touch the
    database. v27.3 savepointed the funding and investor writes and the error
    persisted because five other sites had the same shape.
    """

    def test_no_swallowing_write_remains_without_a_savepoint(self):
        """AST scan for the pattern, so a new one cannot be added quietly.

            with transaction.atomic():
                try:
                    Model.objects.create(...)   # fails
                except Exception:
                    log(...)                    # needs_rollback stays SET
                OtherModel.objects.get(...)     # dies here, blamed here
        """
        import ast
        import pathlib
        writes = ("save", "create", "get_or_create", "update_or_create",
                  "delete", "bulk_create", "update", "add", "set", "remove")
        offenders = []
        for name in ("fundos/profile/services.py", "fundos/profile/records.py"):
            path = pathlib.Path(name)
            if not path.exists():
                continue
            tree = ast.parse(path.read_text())
            for node in ast.walk(tree):
                if not isinstance(node, ast.Try):
                    continue
                guarded = any(
                    isinstance(n, ast.With) and any(
                        "atomic" in ast.dump(i.context_expr)
                        or "_isolated" in ast.dump(i.context_expr)
                        for i in n.items)
                    for n in node.body)
                has_write = any(
                    isinstance(n, ast.Call)
                    and isinstance(n.func, ast.Attribute)
                    and n.func.attr in writes
                    for n in ast.walk(ast.Module(body=node.body,
                                                 type_ignores=[])))
                swallows = any(
                    not any(isinstance(s, ast.Raise) for s in h.body)
                    for h in node.handlers)
                if has_write and swallows and not guarded:
                    offenders.append(f"{name}:{node.lineno}")
        self.assertEqual(offenders, [], f"unsavepointed swallowed writes: {offenders}")

    def test_the_helper_rolls_back_only_its_own_savepoint(self):
        """A failed write inside the helper must not poison the surrounding

        transaction — that is the whole property the sync depended on.
        """
        from fundos.profile.models import CompanyProfile
        from fundos.profile.services import _isolated_write
        before = CompanyProfile.objects.count()

        with _isolated_write("deliberate failure"):
            CompanyProfile.objects.create()   # company_id is NOT NULL

        # The transaction survived: this query would raise
        # TransactionManagementError if needs_rollback were still set, which
        # is exactly how the CKB sync failed on every run since v27.1.
        self.assertEqual(CompanyProfile.objects.count(), before)

    def test_the_judgement_persist_is_savepointed(self):
        """The decisive site: it runs immediately before the sync and matches

        the log timestamp to the second.
        """
        import inspect
        from fundos.profile import services
        src = inspect.getsource(services)
        self.assertIn('_isolated_write("judgement persist")', src)


# ==========================================================================
# Trace hygiene
# ==========================================================================

class TraceFieldsAreParseable(TestCase):
    """19 Aug carried `thinking_budget=2048` on one line and

    `thinking_budget="4000"` on the next, because the value arrived as
    whichever type an administrator had typed and the formatter quotes
    strings. A field that changes shape between lines breaks every parser.
    """

    def test_an_int_budget_renders_as_an_int(self):
        self.assertEqual(
            adapter._budget_str({"thinking": {"budget_tokens": 2048}}), 2048)

    def test_a_string_budget_is_coerced(self):
        self.assertEqual(
            adapter._budget_str({"thinking": {"budget_tokens": "4000"}}), 4000)

    def test_an_absent_budget_is_the_literal_unset(self):
        self.assertEqual(adapter._budget_str({}), "unset")

    def test_junk_does_not_raise(self):
        self.assertEqual(
            adapter._budget_str({"thinking": {"budget_tokens": "lots"}}),
            "unset")


# ==========================================================================
# The diagnostic must be able to answer the question it left open
# ==========================================================================

class DiagnosticCoversTheUnexplainedResult(TestCase):
    """H1 explained the schema roles. It did NOT explain

    `company_profile_deep_extract`, which sent no schema (caps_sent was
    "thinking,web_search") and still searched zero times, while
    `company_profile_section` with identical capabilities searched 2 and 6.
    Context size is ruled out — run 2's extract had a SMALLER context than
    the section calls that searched.

    The remaining difference is the system prompt, so it has to become a
    variable. Otherwise removing the schema will not restore search on the
    role that most needs it, and nobody will know why.
    """

    def test_the_extraction_framing_hypothesis_is_testable(self):
        import pathlib
        src = pathlib.Path("tools/diagnose_gemini_search.py").read_text()
        self.assertIn("EXTRACTION_SYSTEM", src)
        self.assertIn("H4", src)

    def test_the_grid_reports_framing_as_a_variable(self):
        import pathlib
        src = pathlib.Path("tools/diagnose_gemini_search.py").read_text()
        self.assertIn('"FRAMING (H4)"', src)

    def test_the_original_grid_is_still_available(self):
        """16 calls is real quota. `--no-framing` runs the 8-cell version."""
        import pathlib
        src = pathlib.Path("tools/diagnose_gemini_search.py").read_text()
        self.assertIn("--no-framing", src)


# ==========================================================================
# Nothing from v27.4 regressed
# ==========================================================================

class EarlierGuaranteesHold(TestCase):

    def setUp(self):
        ledger.reset()

    def test_thinking_is_still_billed_as_output(self):
        ledger.record(role="r", endpoint_code="GEMINI", model="m",
                      status="success", prompt_tokens=3612,
                      completion_tokens=1391, thinking_tokens=6266,
                      cache_read_tokens=0, cache_write_tokens=0,
                      cost_inr=Decimal("1"), latency_ms=1, context_chars=1,
                      search_count=None, tier="advanced", config_profile="p")
        s = ledger.summarise(ledger.since(0))
        self.assertEqual(s["billed_output_tokens"], 1391 + 6266)

    def test_search_expected_roles_are_still_derived(self):
        from fundos.llm.tier_contract import SEARCH_REQUIRED_ROLES
        self.assertTrue(SEARCH_REQUIRED_ROLES <= adapter.SEARCH_EXPECTED_ROLES)

    def test_the_migration_state_is_clean(self):
        """v27.3 shipped a model change with no migration file, so `migrate`

        reported unapplied changes on deployment and the file had to be
        generated on a production host.
        """
        from io import StringIO

        from django.core.management import call_command
        out = StringIO()
        try:
            call_command("makemigrations", "--check", "--dry-run",
                         stdout=out, stderr=out)
        except SystemExit as e:
            self.fail(f"unmigrated model changes: {out.getvalue()} ({e})")
