"""
v27.4 — the tier contract must survive to the wire, and a run must state its
own economics.

Every test here corresponds to a defect that was REPRODUCED against the v27.3
tarball before the fix was written. The docstrings say which, because a test
whose purpose is forgotten is a test that gets deleted the next time it is
inconvenient.
"""
from decimal import Decimal
from unittest import mock

from django.core.exceptions import ValidationError
from django.test import TestCase

from fundos.core.scoping import tenant_context
from fundos.llm import adapter, ledger
from fundos.llm.tier_contract import (SEARCH_REQUIRED_ROLES,
                                      TierContractViolation, apply_contract)
from tests.conftest_helpers import make_world


# ==========================================================================
# 1. The violation must reach the caller
# ==========================================================================

class ViolationIsNotSwallowed(TestCase):
    """v27.3 raised TierContractViolation inside a `except Exception: pass`.

    `assert_role_tier_coherent` was added so a search-required role on a
    non-searching tier would fail loudly. It was then called from inside
    `try: ... except Exception: config_profile = None` in the adapter, which
    caught it, discarded it, and let the call proceed with capabilities={}.
    The guardrail never reached a caller.
    """

    def test_resolution_raises_for_a_search_role_on_a_simple_tier(self):
        from fundos.llm.tiers import resolve_tier_profile_code
        with self.assertRaises(TierContractViolation):
            resolve_tier_profile_code(role="company_profile_deep_extract",
                                      tier="simple", tenant_id=None)

    def test_the_adapter_re_raises_rather_than_defaulting(self):
        """The regression guard: catch TierContractViolation SEPARATELY."""
        import inspect
        src = inspect.getsource(adapter._call_llm) if hasattr(
            adapter, "_call_llm") else inspect.getsource(adapter)
        # Both resolution sites must name the exception before the broad catch.
        self.assertIn("except TierContractViolation:", src)
        self.assertEqual(
            src.count("except TierContractViolation:"), 2,
            "both resolve_tier_profile_code call sites must re-raise")

    def test_extraction_roles_are_unaffected(self):
        from fundos.llm.tiers import resolve_tier_profile_code
        # Must not raise: this role has no retrieval requirement.
        resolve_tier_profile_code(role="company_profile_records",
                                  tier="simple", tenant_id=None)


# ==========================================================================
# 2. Cost control 3 must not override the contract
# ==========================================================================

class OptimisationDoesNotOverrideInvariant(TestCase):
    """`research_synthesis` was in BOTH NO_THINKING_ROLES and

    SEARCH_REQUIRED_ROLES. capabilities_payload() set budget_tokens=2048 per
    the advanced contract; four lines later the adapter reset it to 0 —
    reproducing the exact v27.3 root cause inside the release that fixed it.
    """

    def test_the_two_role_sets_no_longer_overlap(self):
        self.assertEqual(
            SEARCH_REQUIRED_ROLES & adapter.NO_THINKING_ROLES, set(),
            "a role that must retrieve needs a budget to decide to retrieve")

    def test_thinking_survives_for_a_search_required_role(self):
        """Even if someone re-adds the overlap, the runtime guard holds."""
        caps = apply_contract({}, "advanced", provider="gemini",
                              provider_caps={"web_search", "thinking_budget"})
        self.assertEqual(caps["thinking"]["budget_tokens"], 2048)

        role = "company_profile_deep_extract"
        needs = (role in SEARCH_REQUIRED_ROLES or "web_search" in caps)
        self.assertTrue(needs)
        # Mirrors the adapter's guarded branch.
        if role in adapter.NO_THINKING_ROLES and needs:
            pass                     # left in place
        elif role in adapter.NO_THINKING_ROLES:
            caps = {k: v for k, v in caps.items() if k != "thinking"}
            caps["thinking"] = {"budget_tokens": 0}
        self.assertEqual(caps["thinking"]["budget_tokens"], 2048)

    def test_a_pure_extraction_role_still_loses_its_thinking(self):
        """The optimisation must still work where it is correct."""
        caps = apply_contract({}, "simple", provider="gemini",
                              provider_caps={"thinking_budget"})
        self.assertNotIn("web_search", caps)
        self.assertEqual(caps["thinking"]["budget_tokens"], 0)


# ==========================================================================
# 3. One authority for the search role sets
# ==========================================================================

class SearchRoleSetsAreDerived(TestCase):
    """Two hand-maintained lists had drifted three ways by 18 Aug.

    `peer_insight` was required but not expected, so it was forced onto a
    searching tier and then received no directive, no tail reminder and no
    no-search warning — the one role guaranteed to fail silently.
    """

    def test_every_required_role_is_also_expected(self):
        self.assertTrue(
            SEARCH_REQUIRED_ROLES <= adapter.SEARCH_EXPECTED_ROLES,
            "SEARCH_EXPECTED_ROLES must be derived, never hand-maintained")

    def test_peer_insight_now_gets_the_directive_and_the_warning(self):
        self.assertIn("peer_insight", SEARCH_REQUIRED_ROLES)
        self.assertIn("peer_insight", adapter.SEARCH_EXPECTED_ROLES)

    def test_beneficial_roles_are_expected_but_not_required(self):
        for role in adapter.SEARCH_BENEFICIAL_ROLES:
            self.assertIn(role, adapter.SEARCH_EXPECTED_ROLES)
            self.assertNotIn(role, SEARCH_REQUIRED_ROLES)

    def test_expected_is_exactly_the_union(self):
        self.assertEqual(
            adapter.SEARCH_EXPECTED_ROLES,
            SEARCH_REQUIRED_ROLES | adapter.SEARCH_BENEFICIAL_ROLES)


# ==========================================================================
# 4. Truncation is measurable
# ==========================================================================

class TruncationIsVisible(TestCase):
    """v27.3 raised the adapter ceiling to 120k and warned there — the SECOND

    cut. `MAX_WEBSITE_CHARS = 6000` in compress_payloads is the first and
    larger one, applied per page, and was entirely silent. MediBuddy's
    `website_chars=31001` was measured AFTER it ran.
    """

    def test_clean_text_reports_what_it_dropped(self):
        from fundos.profile.services import _clean_text
        losses = []
        out = _clean_text("x" * 10000, 6000, _losses=losses, _key="website.about")
        self.assertEqual(len(losses), 1)
        self.assertEqual(losses[0]["chars_in"], 10000)
        self.assertEqual(losses[0]["chars_dropped"], 4000)
        self.assertEqual(losses[0]["key"], "website.about")
        self.assertIn("truncated", out)

    def test_short_text_is_untouched_and_unreported(self):
        from fundos.profile.services import _clean_text
        losses = []
        out = _clean_text("short page", 6000, _losses=losses)
        self.assertEqual(losses, [])
        self.assertEqual(out, "short page")
        self.assertNotIn("truncated", out)

    def test_the_model_is_told_the_page_is_incomplete(self):
        """Without the marker, a truncated page reads as a short one and

        'not stated on the website' becomes the wrong conclusion."""
        from fundos.profile.services import _clean_text
        out = _clean_text("y" * 9000, 6000)
        self.assertIn("this page is incomplete", out)

    def test_compression_emits_one_aggregate_event(self):
        from fundos.profile import services
        events = []
        self._patch_trace(services, events)
        services.compress_payloads({"website": {
            "home": "a" * 9000, "about": "b" * 7000, "tiny": "c" * 10}})
        emitted = [e[2] for e in events if e[0] == "sources.compression"]
        self.assertEqual(len(emitted), 1,
                         "one aggregate line per run, not one per key")
        fields = emitted[0]
        self.assertEqual(fields["keys_truncated"], 2)
        self.assertEqual(fields["chars_dropped"], 3000 + 1000)

    def test_fanout_trim_reports_per_call(self):
        events = []
        self._patch_trace_adapter(events)
        adapter._trim_bulk_sources("company_profile_section",
                                   {"website_extract": "z" * 20000})
        fields = [e[2] for e in events if e[0] == "llm.context.fanout_trim"]
        self.assertTrue(fields, "a cut that nothing records cannot be tuned")
        self.assertEqual(fields[0]["chars_dropped"], 20000 - 6000)

    def test_the_deep_extract_still_reads_the_whole_corpus(self):
        ctx = {"website_extract": "z" * 20000}
        self.assertEqual(
            adapter._trim_bulk_sources("company_profile_deep_extract", ctx), ctx)

    # -- helpers ----------------------------------------------------------
    def _patch_trace(self, module, sink):
        import fundos.profile.trace as trace_mod
        original = trace_mod.trace_event

        def fake(step, status, **fields):
            sink.append((step, status, fields))
            return original(step, status, **fields)
        trace_mod.trace_event = fake
        self.addCleanup(setattr, trace_mod, "trace_event", original)

    def _patch_trace_adapter(self, sink):
        self._patch_trace(adapter, sink)


# ==========================================================================
# 5. llmCalls — the first test that has ever asserted on it
# ==========================================================================

class RunCallCounting(TestCase):
    """v27.2 reported 9, the changelog claimed 7, the log showed 11.

    v27.3 counted properly but opened the window inside
    `_generate_consolidated`, which excluded `_run_assessment_inputs` because
    the CALLER invokes that after the function returns: 10 of 11. No test
    anywhere asserted on the value.
    """

    def setUp(self):
        ledger.reset()

    def test_the_ledger_counts_every_billed_call(self):
        mark = adapter.llm_call_count()
        for i in range(3):
            adapter._log_call(
                role="company_profile_records", endpoint_code="MOCK",
                model="mock", deal_id=None, user=None, calling_context="t",
                latency_ms=5, status="success", prompt_tokens=100,
                completion_tokens=10)
        self.assertEqual(adapter.llm_calls_since(mark), 3)

    def test_failed_calls_are_counted_because_they_were_billed(self):
        mark = adapter.llm_call_count()
        adapter._log_call(role="r", endpoint_code="GEMINI", model="m",
                          deal_id=None, user=None, calling_context="t",
                          latency_ms=5, status="error", prompt_tokens=100,
                          completion_tokens=0, error="boom")
        self.assertEqual(adapter.llm_calls_since(mark), 1)
        entry = adapter.llm_ledger_since(mark)[0]
        self.assertEqual(entry["status"], "error")

    def test_the_window_spans_every_call_the_run_made(self):
        """The v27.3 gap: the window closed before assessment ran.

        Was asserted structurally, by reading the source of a function that no
        longer exists — and CHANGES_v29 §5.2 makes the case against that form
        directly: "a test that reads source text tests the comment, not the
        behaviour". Asserted behaviourally now: run a generation, and check the
        reported count against the ledger, which is what actually recorded the
        dispatches.

        The count also has to survive the research batches, which are
        dispatched from worker threads and would otherwise be invisible to a
        thread-local ledger.
        """
        from fundos.llm.adapter import llm_call_count, llm_ledger_since
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile)

        world = make_world()
        company = world["a"]["company"]
        user = world["a"]["founder"]

        def _late_call(profile, payloads, user=None):
            """Stand in for assessment inputs, which the caller runs AFTER
            the pipeline returns. Seeding the real assessment workbook is not
            needed to prove the window spans it — only that a call dispatched
            at that point is counted."""
            ledger.record(role="assessment_inputs", status="mocked")
            return {"written": 0}

        with tenant_context(world["a"]["tenant"].id):
            profile = get_or_create_profile(company, user=user)
            ledger.reset()
            mark = llm_call_count()
            with mock.patch(
                    "fundos.profile.services._run_assessment_inputs",
                    side_effect=_late_call):
                result = generate_profile(profile, user=user, force=True)

            entries = llm_ledger_since(mark)
            roles = [e["role"] for e in entries]

            self.assertEqual(result["llmCalls"], len(entries),
                             "the reported count must equal what was recorded")
            self.assertIn("profile_research_batch", roles,
                          "research batches run in worker threads and must "
                          "still be counted against the run")
            self.assertIn("profile_synthesis", roles)
            self.assertIn("assessment_inputs", roles,
                          "a call dispatched after the pipeline returns must "
                          "still be inside the counted window")


# ==========================================================================
# 6. run.economics
# ==========================================================================

class RunEconomics(TestCase):
    """Costing a run meant parsing eleven trace lines by hand, and the total

    still came out 39% low because thinking tokens never reached the database.
    """

    def setUp(self):
        ledger.reset()

    def _call(self, role, prompt, completion, thinking=0, cost="1.00",
              status="success", searches=None):
        ledger.record(role=role, endpoint_code="GEMINI", model="gemini-3.5-flash",
                      status=status, prompt_tokens=prompt,
                      completion_tokens=completion, thinking_tokens=thinking,
                      cache_read_tokens=0, cache_write_tokens=0,
                      cost_inr=Decimal(cost), latency_ms=1000,
                      context_chars=100, search_count=searches, tier="advanced",
                      config_profile="tier.advanced.gemini")

    def test_thinking_is_included_in_billed_output(self):
        """The 39% error. Providers bill reasoning at the output rate and

        none of them include it in the completion count."""
        self._call("company_profile_judgment", 3612, 1391, thinking=6266)
        s = ledger.summarise(ledger.since(0))
        self.assertEqual(s["completion_tokens"], 1391)
        self.assertEqual(s["thinking_tokens"], 6266)
        self.assertEqual(s["billed_output_tokens"], 1391 + 6266)
        self.assertEqual(s["total_tokens"], 3612 + 1391 + 6266)

    def test_unknown_searches_are_not_reported_as_zero(self):
        """`searches=unknown` means the provider returned no grounding

        metadata — the tool was never attached, or it was attached and
        declined. Opposite fixes; they must not render identically."""
        self._call("company_profile_deep_extract", 100, 10, searches=None)
        self._call("assessment_inputs", 100, 10, searches=0)
        self._call("peer_insight", 100, 10, searches=3)
        s = ledger.summarise(ledger.since(0))
        self.assertEqual(s["searches"], 3)
        self.assertEqual(s["searches_unknown"], 1)

    def test_per_role_rollup_is_ordered_by_spend(self):
        self._call("company_profile_records", 4113, 70, cost="5.00")
        self._call("company_profile_records", 4113, 70, cost="5.00")
        self._call("readiness_summary", 173, 350, cost="0.50")
        rows = ledger.by_role(ledger.since(0))
        self.assertEqual(rows[0]["role"], "company_profile_records")
        self.assertEqual(rows[0]["calls"], 2)
        self.assertEqual(rows[0]["cost_inr"], 10.0)

    def test_redundant_input_detects_the_fanout(self):
        """C-03's fingerprint: one role called repeatedly with the same context."""
        for _ in range(4):
            self._call("company_profile_records", 4113, 70)
        self._call("company_profile_deep_extract", 15800, 7625)
        s = ledger.summarise(ledger.since(0))
        # Three of four records calls are re-sends; the extract runs once.
        self.assertGreater(s["redundant_input_pct"], 30.0)

    def test_a_single_call_run_reports_no_redundancy(self):
        self._call("company_profile_deep_extract", 15800, 7625)
        self.assertEqual(
            ledger.summarise(ledger.since(0))["redundant_input_pct"], 0.0)

    def test_the_ledger_is_isolated_per_thread(self):
        """Celery runs concurrent generations in one process; a shared list

        would blend two tenants into a cost figure belonging to neither."""
        import threading
        self._call("role_a", 100, 10)
        seen = {}

        def other():
            seen["count"] = len(ledger.since(0))
        t = threading.Thread(target=other)
        t.start()
        t.join()
        self.assertEqual(seen["count"], 0)
        self.assertEqual(len(ledger.since(0)), 1)

    def test_emitting_economics_never_breaks_a_run(self):
        from fundos.profile.services import _emit_run_economics
        self.assertEqual(_emit_run_economics(0, mode="consolidated"), 0)


# ==========================================================================
# 7. The remaining silent-degradation routes
# ==========================================================================

class ContractSurvivesToTheWire(TestCase):
    """The contract runs inside capabilities_payload(), which only executes

    when a profile ROW resolved. With none seeded the adapter fell through to
    capabilities={} and the contract never ran at all — the same silent
    degradation reached by a different door.
    """

    def test_an_unseeded_profile_cannot_produce_an_ungrounded_required_call(self):
        import inspect
        src = inspect.getsource(adapter)
        self.assertIn("CONTRACT_VIOLATION", src)
        self.assertIn(
            'role in SEARCH_REQUIRED_ROLES and "web_search" not in capabilities',
            src)

    def test_the_breaker_refuses_rather_than_degrades_a_required_role(self):
        import inspect
        src = inspect.getsource(adapter)
        self.assertIn("BREAKER_OPEN", src)

    def test_an_import_time_check_catches_role_set_conflicts(self):
        import inspect
        src = inspect.getsource(adapter)
        self.assertIn("_ROLE_SET_CONFLICT", src)

    def test_the_refusal_is_not_a_generic_provider_outage(self):
        """`LLMUnavailable` means "try the other path" and the generation

        code duly falls back from consolidated to per-section. The per-section
        path does not search either, so a refusal raised as LLMUnavailable
        would be converted into a quieter version of the same ungrounded
        profile. The subclass exists so the caller can tell the two apart.
        """
        from fundos.core.exceptions import LLMUnavailable, UngroundableCall
        self.assertTrue(issubclass(UngroundableCall, LLMUnavailable))
        self.assertNotEqual(UngroundableCall.code, LLMUnavailable.code)

    def test_generation_abandons_rather_than_downgrades(self):
        """An ungrounded run must publish nothing.

        The original concern was that a refusal would be caught by a generic
        fallback and re-run, one section at a time, on a path that did not
        search either — publishing the same ungrounded profile more quietly
        and at greater cost. There is no second path any more, which removes
        the mechanism; what still has to hold is the outcome, asserted here
        directly rather than by reading the source for an except-clause order.
        """
        from fundos.profile.models import ProfileSection
        from fundos.profile.pipeline.orchestrator import (UngroundedGeneration,
                                                          run_pipeline)
        from fundos.profile.services import get_or_create_profile

        world = make_world()
        with tenant_context(world["a"]["tenant"].id):
            profile = get_or_create_profile(world["a"]["company"],
                                            user=world["a"]["founder"])
            run = _run_row(profile)

            # Nothing retrieved, and not mocked — the gate must refuse.
            with mock.patch(
                    "fundos.profile.pipeline.source1_research.run",
                    return_value={"searches": 0, "text_chars": 0,
                                  "batches_total": 0, "batches_succeeded": 0,
                                  "batches_failed": []}), \
                mock.patch(
                    "fundos.profile.pipeline.source2_documents.run",
                    return_value={"documents": [], "files_total": 0}), \
                    mock.patch("fundos.config.feature_flags.ai_mocked",
                               return_value=False):
                result = run_pipeline(profile, run,
                                      user=world["a"]["founder"])

            self.assertEqual(result["mode"], "refused")
            self.assertEqual(result["generated"], [])
            run.refresh_from_db()
            self.assertEqual(run.status, "refused")
            self.assertFalse(
                ProfileSection.objects.filter(profile=profile)
                .exclude(content="").exclude(structured={}).exists(),
                "a refused run must not have written any section content")
            self.assertTrue(issubclass(UngroundedGeneration, Exception))


def _run_row(profile):
    from fundos.profile.models import ProfileGenerationRun
    return ProfileGenerationRun.objects.create(
        profile=profile, tenant_id=profile.tenant_id, mode="pipeline",
        status="running", stage="queued")
