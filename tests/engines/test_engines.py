"""Pure-engine tests (Doc 8 §6) — fixture inputs → asserted outputs.
No DB required; engines must be importable without Django settings."""
from django.test import SimpleTestCase

from fundos.engines.default_config import DEFAULT_SCORING_CONFIG_V1 as CFG
from fundos.engines.dilution_engine import (
    crossdoc_inconsistencies, derive_investor_categories, dilution_matrix,
    simulate_dilution,
)
from fundos.engines.raise_engine import compute_raise
from fundos.engines.readiness_engine import compute_readiness
from fundos.engines.similarity_engine import compute_similarity
from fundos.engines.valuation_engine import compute_valuation

RICH_CKB = {
    "description": "x", "vision": "x", "problem": "x", "solution": "x",
    "arr": 120_000_000, "revenue": 120_000_000, "burn": 4_000_000,
    "runway_months": 14, "gross_margin": 72, "sector": "SaaS",
    "geography": "India", "segments": ["smb"], "founders": ["a"],
    "leadership": ["a"], "employees": 25, "cap_table": [
        {"holder": "Founders", "pct": 85, "kind": "founder"},
        {"holder": "Angel", "pct": 10, "kind": "investor"},
        {"holder": "ESOP", "pct": 5, "kind": "esop"}],
    "board": ["a"], "incorporation": "2021", "customers": 40, "churn": 2,
    "cac": 50_000, "ltv": 300_000, "growth_rate": 90,
    "founding_year": 2021,
}


class ReadinessEngineTests(SimpleTestCase):
    def test_rich_company_is_ready(self):
        result = compute_readiness(RICH_CKB, materials_count=3, cfg=CFG)
        self.assertGreaterEqual(result.overall_score, 70)
        self.assertEqual(result.overall_band, "Ready")
        self.assertEqual(len(result.dimensions), 6)
        for d in result.dimensions:
            self.assertEqual(d.status, "ready")

    def test_empty_company_not_ready_with_gaps(self):
        result = compute_readiness({}, materials_count=0, cfg=CFG)
        self.assertEqual(result.overall_band, "Not yet ready")
        self.assertTrue(result.missing)
        self.assertTrue(result.recommendations)

    def test_deterministic(self):
        a = compute_readiness(RICH_CKB, 1, CFG)
        b = compute_readiness(RICH_CKB, 1, CFG)
        self.assertEqual(a.overall_score, b.overall_score)


class SimilarityEngineTests(SimpleTestCase):
    def test_identical_company_scores_high(self):
        company = {"sector": "SaaS", "subsector": "Vertical",
                   "business_model": "b2b", "revenue": 100, "growth_rate": 80,
                   "geography": "India", "funding_stage": "seed",
                   "team_size": 20, "customer_profile": "smb"}
        result = compute_similarity(company, dict(company),
                                    CFG["similarity_weights"])
        self.assertGreater(result.score, 85)
        self.assertIn("sector_subsector", result.breakdown)

    def test_unrelated_company_scores_low(self):
        a = {"sector": "SaaS", "revenue": 100, "geography": "India"}
        b = {"sector": "Mining", "revenue": 1_000_000, "geography": "US"}
        result = compute_similarity(a, b, CFG["similarity_weights"])
        self.assertLess(result.score, 40)

    def test_breakdown_explains_score(self):
        a = {"sector": "SaaS", "geography": "India"}
        b = {"sector": "SaaS", "geography": "India"}
        result = compute_similarity(a, b, CFG["similarity_weights"])
        total = sum(v["contribution"] for v in result.breakdown.values())
        self.assertAlmostEqual(total, result.score, delta=0.5)


class RaiseEngineTests(SimpleTestCase):
    def test_three_scenarios_and_ranges(self):
        result = compute_raise(RICH_CKB, {"runway": "18m", "timeline": "6m"},
                               {"median_raise_usd": 2_000_000}, CFG)
        self.assertEqual(len(result.scenarios), 3)
        labels = [s.label for s in result.scenarios]
        self.assertEqual(labels, ["Conservative", "Recommended", "Aggressive"])
        self.assertLess(result.low_usd, result.recommended_usd)
        self.assertLess(result.recommended_usd, result.high_usd)
        self.assertLess(result.dilution_low_pct, result.dilution_high_pct + 0.01)

    def test_max_dilution_cap_respected(self):
        result = compute_raise(RICH_CKB, {"runway": "18m",
                                          "maxDilutionPct": 12}, {}, CFG)
        self.assertLessEqual(result.dilution_high_pct, 12)

    def test_no_burn_lowers_confidence(self):
        ckb = dict(RICH_CKB)
        ckb["burn"] = None
        result = compute_raise(ckb, {"runway": "18m"}, {}, CFG)
        self.assertEqual(result.confidence, "low")
        self.assertTrue(result.risks)


class ValuationEngineTests(SimpleTestCase):
    def test_range_and_football_field(self):
        peers = {"stage_postmoney_median_usd": 10_000_000,
                 "trading_valuations_usd": [6e6, 9e6, 12e6, 15e6]}
        result = compute_valuation(RICH_CKB, peers, CFG)
        self.assertGreater(result.range_low, 0)
        self.assertGreater(result.range_high, result.range_low)
        methods = [m.method for m in result.football_field]
        self.assertIn("EV/ARR", methods)
        self.assertTrue(any("FEMA" in l for l in result.limitations))

    def test_outliers_visible_not_hidden(self):
        peers = {"stage_postmoney_median_usd": 10_000_000,
                 "trading_valuations_usd": [6e6, 9e6, 12e6]}
        ckb = dict(RICH_CKB)
        ckb["growth_rate"] = 200   # inflates DCF into outlier territory
        result = compute_valuation(ckb, peers, CFG)
        # Every method (outlier or not) stays in the football field.
        self.assertGreaterEqual(len(result.football_field), 5)
        for m in result.football_field:
            if m.is_outlier:
                self.assertTrue(m.outlier_reason)

    def test_no_data_low_confidence(self):
        result = compute_valuation({}, {}, CFG)
        self.assertEqual(result.confidence, "low")


class DilutionEngineTests(SimpleTestCase):
    def test_equity_dilution_bands(self):
        result = simulate_dilution(2_000_000, 8_000_000, 12_000_000,
                                   "equity", cap_table=[
                                       {"holder": "F", "pct": 100,
                                        "kind": "founder"}], cfg=CFG)
        low, high = result.ownership_bands["newInvestor"]
        self.assertLess(low, high)                     # a RANGE, never a point
        # 2 on 8 pre → 20%; 2 on 12 pre → ~14.3%
        self.assertAlmostEqual(high, 20.0, delta=0.1)
        self.assertAlmostEqual(low, 14.29, delta=0.1)

    def test_safe_conversion_at_boundaries(self):
        result = simulate_dilution(1_000_000, 8_000_000, 12_000_000,
                                   "safe", cfg=CFG)
        self.assertIn("discount_pct", result.conversion_assumptions)
        eq = simulate_dilution(1_000_000, 8_000_000, 12_000_000,
                               "equity", cfg=CFG)
        # Discounted conversion → more dilution than a priced round.
        self.assertGreater(result.ownership_bands["newInvestor"][1],
                           eq.ownership_bands["newInvestor"][1])

    def test_matrix_grid(self):
        grid = dilution_matrix([1e6, 2e6], [8e6, 12e6], esop_topup_pct=10)
        self.assertEqual(len(grid), 4)
        for cell in grid:
            self.assertGreater(cell["dilutionPct"], 0)
            self.assertLess(cell["founderOwnershipPct"], 100)

    def test_investor_categories_by_band(self):
        rows = [{"round_type": "seed", "band_low_usd": 250_000,
                 "band_high_usd": 3_000_000, "primary": ["Seed VCs"],
                 "secondary": ["Angels"], "optional": ["Corporate VCs"]}]
        result = derive_investor_categories("seed", 1_500_000, rows)
        self.assertEqual(result["primary"], ["Seed VCs"])

    def test_crossdoc_inconsistency_flagged(self):
        values = {"arr": [{"document": "ckb", "value": 100},
                          {"document": "model", "value": 130}]}
        findings = crossdoc_inconsistencies(values, {"arr": 0.02})
        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0]["field_key"], "arr")

    def test_crossdoc_within_tolerance_passes(self):
        values = {"arr": [{"document": "ckb", "value": 100},
                          {"document": "model", "value": 101}]}
        findings = crossdoc_inconsistencies(values, {"arr": 0.02})
        self.assertEqual(findings, [])
