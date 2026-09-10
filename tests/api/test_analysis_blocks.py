"""Red flags, data gaps and band recommendations — three blocks, one basis.

The payload used to carry one merged `diligence_findings` list and a
`recommendations` list computed against a baseline it never showed. Both were
wrong in ways a reader could see:

  * The merged list was ranked, so a company whose IP claim contradicted its
    own patent record showed two routine "no value established" rows instead
    and never mentioned the contradiction at all.
  * `overallIfReached` read 6.84 beside a headline of 6.97, because the
    recommendations rolled up over all seven categories — including G, the
    weakest one — while the headline rolled up over A-F.

A red flag is "we looked and something is wrong". A data gap is "we have not
found it yet". They go to different people and they are now different lists.
"""
from decimal import Decimal

from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base


class AnalysisBase(Phase1Base):

    def payload(self):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def analysis(self):
        return self.payload()["analysis"]

    def contradict(self):
        """Score a moat row Excellent while its cross-check says Poor."""
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        for key, value, band in (("ANC_MOAT_IP", "Excellent", "Excellent"),
                                 ("BQ_PATENTS", "0", "")):
            ParameterValue.objects.update_or_create(
                assessment=self.assessment, input_key=key,
                defaults={"raw_value": value, "band": band, "category": "C",
                          "source_type": "document", "source_tier": 1,
                          "confidence": Decimal("0.9"),
                          "justification": f"Read for {key}."})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()


class TheBlocksAreThree(AnalysisBase):

    def test_the_analysis_carries_the_three_named_blocks(self):
        analysis = self.analysis()
        self.assertEqual(sorted(analysis),
                         ["band_recommendations", "data_gaps", "red_flags"])

    def test_the_old_merged_keys_are_gone(self):
        analysis = self.analysis()
        self.assertNotIn("diligence_findings", analysis)
        self.assertNotIn("recommendations", analysis)

    def test_a_red_flag_carries_exactly_the_agreed_fields(self):
        self.contradict()
        flags = self.analysis()["red_flags"]
        self.assertTrue(flags)
        for flag in flags:
            self.assertEqual(sorted(flag),
                             ["ask", "couldChangeRating", "headline",
                              "inputKey", "parameter", "ref", "severity"])

    def test_a_data_gap_carries_exactly_the_agreed_fields(self):
        for gap in self.analysis()["data_gaps"]:
            self.assertEqual(sorted(gap),
                             ["ask", "couldChangeRating", "headline",
                              "inputKey", "parameter", "ref", "severity"])

    def test_a_recommendation_carries_exactly_the_agreed_fields(self):
        for row in self.analysis()["band_recommendations"]["items"]:
            self.assertEqual(sorted(row),
                             ["currentBand", "displayValue", "inputKey",
                              "overallImpact", "parameter", "ref", "target",
                              "targetBand"])


class RedFlagsMeanSomethingIsWrong(AnalysisBase):

    def test_a_contradiction_is_a_red_flag_and_reaches_the_payload(self):
        self.contradict()
        flags = {f["ref"]: f for f in self.analysis()["red_flags"]}
        self.assertIn("C.3.a", flags,
                      "the IP contradiction never reached the payload")
        flag = flags["C.3.a"]
        self.assertEqual(flag["severity"], "High")
        self.assertEqual(flag["parameter"], "Proprietary IP & Technology")
        self.assertIn("cross-check", flag["headline"])

    def test_a_poor_band_is_a_red_flag_even_with_no_finding_against_it(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="ANC_CXO_FINANCE",
            defaults={"raw_value": "Poor", "band": "Poor", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9"),
                      "justification": "No finance leader in place."})
        run_scoring(self.assessment)

        flags = {f["ref"]: f for f in self.analysis()["red_flags"]}
        self.assertIn("A.2.c", flags)
        self.assertEqual(flags["A.2.c"]["parameter"], "Finance Leadership")
        # An anchor row's value IS its band; "scored Poor (Poor)" says it twice.
        self.assertEqual(flags["A.2.c"]["headline"],
                         "Finance Leadership scored Poor.")

    def test_a_blank_row_is_never_a_red_flag(self):
        """The distinction the whole split exists for."""
        analysis = self.analysis()
        gap_refs = {g["ref"] for g in analysis["data_gaps"]}
        flag_refs = {f["ref"] for f in analysis["red_flags"]}
        self.assertTrue(gap_refs, "the fixture has unanswered rows")
        self.assertFalse(gap_refs & flag_refs,
                         "a row is either unknown or wrong, never both")

    def test_the_ask_never_repeats_the_headline(self):
        self.contradict()
        for flag in self.analysis()["red_flags"]:
            head = flag["headline"].rstrip(".").casefold()
            self.assertNotIn(head, flag["ask"].casefold(), flag["ref"])

    def test_the_ask_never_repeats_itself(self):
        self.contradict()
        for flag in self.analysis()["red_flags"]:
            parts = [p.strip().casefold()
                     for p in flag["ask"].split(". ") if p.strip()]
            self.assertEqual(len(parts), len(set(parts)), flag["ref"])

    def test_flags_are_ordered_by_severity_then_by_what_moves_the_rating(self):
        self.contradict()
        rank = {"High": 0, "Medium": 1, "Low": 2}
        flags = self.analysis()["red_flags"]
        keys = [(rank[f["severity"]], not f["couldChangeRating"])
                for f in flags]
        self.assertEqual(keys, sorted(keys))


class DataGapsAreComplete(AnalysisBase):

    def test_every_unanswered_scoring_row_is_listed(self):
        """Not a top-N. Truncation is how Client Concentration disappeared."""
        data = self.payload()
        gaps = {g["ref"] for g in data["analysis"]["data_gaps"]}

        unanswered = set()

        def walk(node):
            if node.get("isTerminal"):
                if any(f.get("findingType") == "Data Gap"
                       for f in node.get("diligenceFindings") or []):
                    unanswered.add(node["ref"])
            for child in (node.get("subitems") or node.get("children")
                          or []):
                walk(child)

        for cat in data["categories"]:
            walk(cat)
        self.assertTrue(unanswered)
        self.assertEqual(gaps, unanswered)

    def test_a_gap_names_the_parameter_by_its_hierarchy_name(self):
        gaps = {g["ref"]: g for g in self.analysis()["data_gaps"]}
        if "A.1.b" in gaps:
            self.assertEqual(gaps["A.1.b"]["parameter"],
                             "Founder Industry Network")
        for gap in gaps.values():
            self.assertIn(gap["parameter"], gap["headline"])


class TheRecommendationMathsAgreesWithTheHeadline(AnalysisBase):

    def test_the_baseline_is_the_headline(self):
        """The guard against the 6.84-beside-6.97 defect."""
        data = self.payload()
        self.assertEqual(
            data["analysis"]["band_recommendations"]["baseline"]["overall"],
            data["summary"]["overall_score"])
        self.assertEqual(
            data["analysis"]["band_recommendations"]["baseline"]["rating"],
            data["summary"]["deal_rating"])

    def test_reaching_a_higher_band_never_lowers_the_score(self):
        for row in self.analysis()["band_recommendations"]["items"]:
            self.assertGreater(row["overallImpact"], 0, row["ref"])

    def test_the_impact_is_measured_on_the_a_to_f_basis(self):
        """G is excluded from the roll-up, so it is excluded from the delta."""
        from fundos.assessment import hierarchy as H
        from fundos.assessment.phase1 import Reference, rescore_with

        data = self.payload()
        block = data["analysis"]["band_recommendations"]
        if not block["items"]:
            self.skipTest("nothing in a recommendable band in this fixture")
        row = block["items"][0]

        from fundos.assessment.models import ConfigParameter

        # A ParameterValue carries no ref_code unless the bridge wrote it, so
        # the ref resolves through config — scoring row first, as the payload
        # itself does.
        cfg = ConfigParameter.objects.filter(
            ref_code=row["ref"], tenant_id__isnull=True, is_active=True
        ).order_by("-feeds_score", "sort_order", "input_key").first()
        self.assertIsNotNone(cfg, row["ref"])

        ref = Reference(self.assessment)
        target = ref.band_score(row["targetBand"])
        after = rescore_with(self.assessment, ref, cfg.input_key, target,
                             include_codes=H.CATEGORY_CODES)
        self.assertAlmostEqual(
            row["overallImpact"],
            round(after - block["baseline"]["overall"], 3), places=2)

    def test_recommendations_use_the_hierarchy_name(self):
        names = {r["ref"]: r["parameter"]
                 for r in self.analysis()["band_recommendations"]["items"]}
        from fundos.assessment import hierarchy as H
        for ref, name in names.items():
            self.assertEqual(name, H.NAMES[ref], ref)

    def test_no_raw_float_reaches_the_display_value(self):
        for row in self.analysis()["band_recommendations"]["items"]:
            decimals = row["displayValue"].split(".")
            if len(decimals) > 1:
                digits = "".join(c for c in decimals[1] if c.isdigit())
                self.assertLessEqual(len(digits), 2, row["displayValue"])

    def test_an_anchor_row_shows_its_band_not_the_word_band(self):
        for row in self.analysis()["band_recommendations"]["items"]:
            self.assertNotIn("Band", row["displayValue"], row["ref"])

    def test_only_controllable_rows_are_recommended(self):
        """A percentile nobody can move is not advice."""
        refs = {r["ref"]
                for r in self.analysis()["band_recommendations"]["items"]}
        for structural in ("E.2", "E.3", "E.6", "F.2", "F.3", "F.6"):
            self.assertNotIn(structural, refs)


class NoGInTheAnalysis(AnalysisBase):

    def test_no_mandate_row_reaches_any_of_the_three_blocks(self):
        import json
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="IB_GTM_WEEKS",
            defaults={"raw_value": "40", "category": "G",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)

        blob = json.dumps(self.analysis(), default=str)
        self.assertNotIn("IB_GTM_WEEKS", blob)
        self.assertNotIn('"G.', blob)
        self.assertNotIn("Mandate", blob)
