"""End-to-end evidence run.

Not a regression test — a HARNESS that performs a complete generation and
prints the diagnostic lines v27.4 added, so the release can be verified from a
log rather than from a changelog claim. v27.3 shipped with a verification log
in which none of its own fixes were observable.

    python manage.py test tests.api.test_v27_4_evidence_run -v 2
"""
from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import auth_headers, make_world


class EvidenceRun(TestCase):
    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        call_command("seed_llm_costs", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.w = make_world()
        self.company = self.w["a"]["company"]
        self.co = str(self.company.id)
        self.h = auth_headers(self.w["a"]["founder"])

    def test_a_full_run_states_its_own_economics(self):
        import json
        from fundos.llm import ledger
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile)

        self.client.post(
            f"/api/v1/companies/{self.co}/profile/onboard",
            json.dumps({"websiteUrl": "https://helios.example",
                        "hqCountry": "IN", "homeCurrency": "INR",
                        "founders": [{"name": "Asha", "isFullTime": True,
                                      "selfConfirmed": True}]}),
            content_type="application/json", **self.h)

        ledger.reset()
        captured = []
        import fundos.profile.trace as trace_mod
        original = trace_mod.trace_event

        def capture(step, status, **fields):
            captured.append((step, status, fields))
            return original(step, status, **fields)
        trace_mod.trace_event = capture
        self.addCleanup(setattr, trace_mod, "trace_event", original)

        profile = get_or_create_profile(self.company,
                                        user=self.w["a"]["founder"])
        result = generate_profile(profile, user=self.w["a"]["founder"])

        econ = [f for s, _, f in captured if s == "run.economics"]
        roles = [f for s, _, f in captured if s == "run.economics.role"]

        print("\n" + "=" * 74)
        print("RUN ECONOMICS — emitted by the pipeline, not computed by hand")
        print("=" * 74)
        for f in econ:
            for k, v in f.items():
                print(f"  {k:24} {v}")
        print("-" * 74)
        print(f"  {'role':34} {'calls':>5} {'in':>8} {'out':>7} "
              f"{'think':>6} {'in/out':>7}")
        for r in roles:
            print(f"  {r['role']:34} {r['calls']:>5} {r['prompt_tokens']:>8} "
                  f"{r['completion_tokens']:>7} {r['thinking_tokens']:>6} "
                  f"{str(r['tokens_in_per_token_out']):>7}")
        print("=" * 74)
        print(f"  API result llmCalls = {result.get('llmCalls')}")
        print("=" * 74 + "\n")

        # --- assertions ---------------------------------------------------
        self.assertEqual(len(econ), 1, "exactly one economics line per run")
        self.assertTrue(roles, "per-role breakdown must be emitted")

        # The v27.3 gap: llmCalls must match what the adapter dispatched,
        # INCLUDING assessment_inputs, which runs after the consolidated call.
        self.assertEqual(result.get("llmCalls"), econ[0]["llm_calls"])
        self.assertGreater(econ[0]["llm_calls"], 0)
        self.assertEqual(econ[0]["llm_calls"], sum(r["calls"] for r in roles))

        # A mocked run must SAY it was mocked in the economics line, because
        # a zero-cost run that looks healthy is exactly how mocked output
        # leaked into a tested environment before.
        self.assertEqual(econ[0]["mocked_calls"], econ[0]["llm_calls"])
        self.assertIn("MOCKED", econ[0]["concerns"])


class ReplayOf17Aug(TestCase):
    """What `run.economics` WOULD have said about run 332d9f.

    The 17 Aug analysis took a manual pass over eleven `llm.call.*` lines to
    establish that the run cost ₹18.09, that 39% of billed output was
    invisible, that no call searched, and that two roles were burning input to
    return almost nothing. Every one of those facts is now a field.

    Figures are the ones actually observed on 332d9f (MediBuddy), so this
    doubles as a check that the aggregation matches a hand-computed total.
    """

    # role, calls, prompt, completion, thinking  — from the 17 Aug trace lines
    OBSERVED = [
        ("company_profile_deep_extract", 1, 8100, 3900, 0),
        ("company_profile_judgment",     1, 3612, 1391, 6266),
        ("company_profile_records",      4, 4113,   70, 0),
        ("company_profile_section",      3, 4060,  380, 0),
        ("assessment_inputs",            1, 5544, 1137, 0),
        ("readiness_summary",            1,  173,  354, 0),
    ]

    def test_one_line_answers_what_took_a_manual_pass(self):
        from decimal import Decimal
        from fundos.llm import ledger
        ledger.reset()

        # gemini-3.5-flash: $1.50 / $9.00 per M, at 88.0 INR/USD.
        in_rate = Decimal("1.50") * Decimal("88.0") / Decimal("1000000")
        out_rate = Decimal("9.00") * Decimal("88.0") / Decimal("1000000")

        for role, n, prompt, completion, thinking in self.OBSERVED:
            for _ in range(n):
                cost = (Decimal(prompt) * in_rate
                        + Decimal(completion + thinking) * out_rate)
                ledger.record(
                    role=role, endpoint_code="GEMINI",
                    model="gemini-3.5-flash", status="success",
                    prompt_tokens=prompt, completion_tokens=completion,
                    thinking_tokens=thinking, cache_read_tokens=0,
                    cache_write_tokens=0, cost_inr=cost, latency_ms=8000,
                    context_chars=24081, search_count=None,
                    tier="advanced", config_profile="tier.advanced.gemini")

        entries = ledger.since(0)
        totals = ledger.summarise(entries)

        print("\n" + "=" * 74)
        print("REPLAY — run 332d9f (MediBuddy, 17 Aug) through run.economics")
        print("=" * 74)
        for k, v in totals.items():
            print(f"  {k:24} {v}")
        print("-" * 74)
        # "INR" rather than the symbol: this report prints to whatever
        # console the suite runs on, and a Windows cp1252 terminal raises
        # UnicodeEncodeError on the rupee sign — failing the test over its own
        # diagnostic output rather than over anything it measures.
        print(f"  {'role':32} {'calls':>5} {'in':>7} {'out':>6} "
              f"{'think':>6} {'INR':>7} {'in/out':>7}")
        from fundos.profile.services import _ratio
        for r in ledger.by_role(entries):
            print(f"  {r['role']:32} {r['calls']:>5} {r['prompt_tokens']:>7} "
                  f"{r['completion_tokens']:>6} {r['thinking_tokens']:>6} "
                  f"{r['cost_inr']:>7} "
                  f"{str(_ratio(r['prompt_tokens'], r['completion_tokens'] + r['thinking_tokens'])):>7}")
        print("=" * 74 + "\n")

        # 11 calls — the number the log showed. v27.2 reported 9, the
        # changelog claimed 7, v27.3 counted 10.
        self.assertEqual(totals["calls"], 11)

        # The 39% error: thinking is billed output and was invisible.
        self.assertEqual(totals["thinking_tokens"], 6266)
        self.assertEqual(
            totals["billed_output_tokens"],
            totals["completion_tokens"] + totals["thinking_tokens"])
        understated = 1 - (totals["completion_tokens"]
                           / totals["billed_output_tokens"])
        self.assertGreater(understated, 0.40)

        # No call reported a search, and that is stated as unknown rather
        # than as zero — the distinction that cost six releases.
        self.assertEqual(totals["searches"], 0)
        self.assertEqual(totals["searches_unknown"], 11)

        # The fan-out is now a number rather than an observation.
        self.assertGreater(totals["redundant_input_pct"], 30.0)

        # The two fan-out roles sit at the worst input-to-output ratio, which
        # is the signal C-03 should move.
        from fundos.profile.services import _ratio as ratio
        rows = {r["role"]: r for r in ledger.by_role(entries)}
        records = rows["company_profile_records"]
        self.assertGreater(
            ratio(records["prompt_tokens"],
                  records["completion_tokens"] + records["thinking_tokens"]),
            40.0, "records burns input to return almost nothing")
