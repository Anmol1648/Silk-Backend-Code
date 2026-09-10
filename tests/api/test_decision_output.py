"""Turning a scored assessment into a decision, rather than into a list.

Four separate claims the output used to get wrong: that two different bands
are a contradiction, that evidence for a related fact evidences the fact being
scored, that every discrepancy is worth a partner's attention, and that a
score alone is a recommendation.
"""
from decimal import Decimal

from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base


class DifferentBandsAreNotAContradiction(Phase1Base):
    """A contradiction is two readings of ONE fact that disagree."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        # The reported counter-example, verbatim: education Excellent and
        # prior-startup Fair. Two different facts about one founder, both
        # true, cross-checking nothing.
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="ANC_FDR_EDU",
            defaults={"band": "Excellent", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="ANC_PRIOR_STARTUP",
            defaults={"band": "Fair", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def test_a_two_band_gap_between_unrelated_rows_is_not_flagged(self):
        contradictions = [f for f in self.assessment.audit_findings
                          if f.get("check") == "ref_contradiction"]
        named = {k for f in contradictions for k in f["parameters"]}
        self.assertNotIn("ANC_FDR_EDU", named)
        self.assertNotIn("ANC_PRIOR_STARTUP", named)


class EvidenceMustProveTheClaimBeingScored(Phase1Base):
    """Score the claim being measured, not a claim next to it."""

    def _finding(self, justification):
        from fundos.assessment import decision
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_MARQUEE_CLIENTS",
            defaults={"raw_value": "13", "band": "Excellent", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9"),
                      "justification": justification})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()
        return {f["inputKey"]: f
                for f in decision.attribution_findings(self.assessment)}

    def test_client_existence_does_not_evidence_founder_attribution(self):
        """13 named logos prove 13 clients. A.1.b scores something else."""
        findings = self._finding(
            "Named clients include IBM, DHL, Godrej, Pfizer, Zydus, "
            "Max Life Insurance and HDFC Ergo.")

        self.assertIn("TEAM_MARQUEE_CLIENTS", findings)
        finding = findings["TEAM_MARQUEE_CLIENTS"]
        self.assertEqual(finding["severity"], "high")
        self.assertEqual(finding["check"], "unsupported_claim")
        self.assertIn("founder", finding["ask"].lower())

    def test_evidence_that_states_the_link_is_accepted(self):
        findings = self._finding(
            "Both insurance accounts were introduced by the founder, who "
            "knew the CEO from her Deloitte years.")
        self.assertNotIn("TEAM_MARQUEE_CLIENTS", findings)

    def test_the_row_is_held_unscored_until_the_link_is_evidenced(self):
        """Not Evidenced, not Poor.

        An unproven claim is an absent fact, so the row drops out of the
        roll-up and its weight redistributes to the rows that WERE evidenced.
        Banding it Poor would score the company down for something nobody
        established either way.
        """
        findings = self._finding("Named clients include IBM, DHL and Godrej.")
        pv = self.assessment.parameter_values.get(
            input_key="TEAM_MARQUEE_CLIENTS")

        self.assertFalse(pv.band)
        self.assertIsNone(pv.score)
        # The reason survives: a reader is told the link was never evidenced,
        # not the useless "no value was established".
        self.assertTrue(findings["TEAM_MARQUEE_CLIENTS"]["heldUnscored"])
        self.assertIn("held unscored",
                      findings["TEAM_MARQUEE_CLIENTS"]["observation"])

    def test_an_override_is_respected(self):
        """A human who has taken responsibility for the number keeps it."""
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_MARQUEE_CLIENTS",
            defaults={"raw_value": "13", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9"),
                      "justification": "Clients include IBM and DHL.",
                      "is_overridden": True,
                      "override_comment": "Confirmed on the founder call."})
        run_scoring(self.assessment)
        pv = self.assessment.parameter_values.get(
            input_key="TEAM_MARQUEE_CLIENTS")
        self.assertIsNotNone(pv.band)


class FindingsAreAShortlist(Phase1Base):
    """A partner is asked to act on a handful, not on everything found."""

    def _analysis(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["analysis"]

    def test_red_flags_stay_a_handful(self):
        """A partner acts on a few. Nothing caps this but reality — and that
        is the point: a red flag list long enough to need truncating means
        the triggers are wrong, not that the list should be cut."""
        analysis = self._analysis()
        self.assertLessEqual(len(analysis["red_flags"]), 12)

    def test_both_lists_are_present_and_separate(self):
        """A blank row and a wrong row are different problems."""
        analysis = self._analysis()
        self.assertIn("red_flags", analysis)
        self.assertIn("data_gaps", analysis)
        flags = {f["ref"] for f in analysis["red_flags"]}
        gaps = {g["ref"] for g in analysis["data_gaps"]}
        self.assertFalse(flags & gaps)

    def test_each_row_carries_what_a_decision_needs(self):
        analysis = self._analysis()
        for row in analysis["red_flags"] + analysis["data_gaps"]:
            for field in ("ref", "parameter", "headline", "ask", "severity"):
                self.assertTrue(row[field], f"{row['ref']} has no {field}")
            self.assertIn(row["severity"], {"High", "Medium", "Low"})
            self.assertIn("couldChangeRating", row)

    def test_rows_are_ranked_by_decision_relevance(self):
        """Severity first, then whether resolving it moves the rating."""
        for key in ("red_flags", "data_gaps"):
            rows = self._analysis()[key]
            order = [({"High": 0, "Medium": 1, "Low": 2}[r["severity"]],
                      not r["couldChangeRating"]) for r in rows]
            self.assertEqual(order, sorted(order), key)


class RecommendationWeighsEvidenceNotOnlyScore(Phase1Base):
    """A high score must not carry an unsupported claim past diligence."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_MARQUEE_CLIENTS",
            defaults={"raw_value": "13", "band": "Excellent", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9"),
                      "justification": "Clients include IBM, DHL and Godrej."})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _analysis(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        return self.client.get(url, **self.headers).data["analysis"]

    def _verdict(self):
        from fundos.assessment import decision
        # The verdict is capped by evidence problems, which is what a red
        # flag is; a blank row is not a reason to cap a rating.
        return decision.final_recommendation(
            self.assessment, self._analysis()["red_flags"])

    def _analysis_top(self):
        return self._analysis()["red_flags"]

    def test_an_unsupported_claim_caps_the_recommendation(self):
        verdict = self._verdict()
        self.assertTrue(verdict["capped"])
        self.assertNotEqual(verdict["recommendedRating"],
                            verdict["systemRating"])
        self.assertTrue(verdict["capReasons"])
        # The claim caps the rating whether or not it ranked in the top seven:
        # ranking decides what to ask next, never whether the rating stands.
        self.assertIn("TEAM_MARQUEE_CLIENTS",
                      verdict["evidenceQuality"]["unsupportedClaims"])
        self.assertNotIn("TEAM_MARQUEE_CLIENTS",
                         [f["inputKey"]
                          for f in self._analysis_top()])

    def test_the_engine_s_own_rating_is_reported_unchanged(self):
        """The cap is stated beside the score, never applied to it."""
        verdict = self._verdict()
        self.assertEqual(verdict["systemRating"], self.assessment.rating_band)
        self.assertEqual(verdict["headlineScore"],
                         float(self.assessment.overall_score))

    def test_the_statement_says_the_gap_is_evidence_not_performance(self):
        verdict = self._verdict()
        self.assertIn("evidence", verdict["statement"].lower())

    def test_a_clean_deal_is_not_capped(self):
        from fundos.assessment import decision

        verdict = decision.final_recommendation(self.assessment, [])
        # With no findings and coverage above the floor, nothing caps it.
        if verdict["evidenceQuality"]["coveragePct"] >= 60:
            self.assertFalse(verdict["capped"])
            self.assertEqual(verdict["recommendedRating"],
                             verdict["systemRating"])
