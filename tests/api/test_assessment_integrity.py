"""The evidence-quality audit block, and the rating-gap arithmetic beside it.

These run against the REAL seeded scoring model for the same reason the Phase 1
tests do: a check that fires on a fixture cut down to three parameters proves
the predicate works and proves nothing about whether it fires on the eighty-row
model the product actually scores. Every assertion below therefore names a
parameter the platform seeder ships, and the bands come out of the shipped
Series A cut-points rather than out of a stub.

Each test builds the ONE condition it is about and asserts on that check's own
code. Asserting on the finding count instead would make every one of these
tests fail the next time a different check is added, which is the failure mode
that teaches people to delete assertions.
"""
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import make_world


class IntegrityBase(TestCase):
    """One assessment, with a helper to give it exactly the rows a test needs."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)

    def setUp(self):
        from fundos.assessment.models import Assessment

        self.world = make_world()
        self.company = self.world["a"]["company"]
        self.assessment = Assessment.objects.create(
            company=self.company, deal=self.world["a"]["deal"],
            tenant_id=self.world["a"]["tenant"].id,
            deal_stage="Series A", sector="Ecommerce",
            sub_sector="B2B Ecommerce")

    def value(self, key, raw, *, tier=1, confidence="0.95", detail="deck p3",
              justification="", band=""):
        from fundos.assessment.models import ConfigParameter, ParameterValue

        cfg = ConfigParameter.objects.filter(
            input_key=key, tenant_id__isnull=True, is_active=True).first()
        return ParameterValue.objects.create(
            assessment=self.assessment, input_key=key, raw_value=raw,
            band=band, ref_code=(cfg.ref_code if cfg else ""),
            category=(cfg.category_code if cfg else ""),
            unit=(cfg.unit if cfg else ""), source_type="document",
            source_tier=tier, source_detail=detail,
            confidence=Decimal(confidence), justification=justification)

    def findings(self):
        """Score, then return the integrity findings keyed by check code."""
        from fundos.assessment.services import run_scoring

        run_scoring(self.assessment)
        self.assessment.refresh_from_db()
        return {f["check"]: f for f in self.assessment.audit_findings
                if f.get("source") == "integrity"}

    def sector_row(self, **kw):
        from fundos.assessment.models import SectorDealData

        kw.setdefault("level", "sector")
        kw.setdefault("name", "Ecommerce")
        kw.setdefault("deal_count", 120)
        return SectorDealData.objects.create(**kw)


class ValueProvenanceTests(IntegrityBase):
    """Checks about whether a scored figure can be traced back to anything."""

    def test_a_scored_value_naming_no_source_is_reported(self):
        self.value("TEAM_FDR_EXP", "12", detail="")
        finding = self.findings().get("uncited_value")
        self.assertIsNotNone(finding, "an uncited scored value must be named")
        self.assertEqual(finding["severity"], "warning")
        # The message has to name the row, or it is not actionable.
        self.assertIn("A.1.d", finding["message"])

    def test_a_cited_value_is_not_reported(self):
        self.value("TEAM_FDR_EXP", "12", detail="Investor deck, slide 4")
        self.assertNotIn("uncited_value", self.findings())

    def test_an_override_is_not_asked_to_cite_a_document(self):
        """A reviewer's own number has a written reason, not a source page."""
        pv = self.value("TEAM_FDR_EXP", "12", detail="")
        pv.is_overridden = True
        pv.override_comment = "Sector experience, not general experience."
        pv.score = Decimal("5")
        pv.band = "Fair"
        pv.save()
        self.assertNotIn("uncited_value", self.findings())

    def test_a_derived_figure_with_no_working_is_reported(self):
        # Tier 3 projects to Estimate — a figure built rather than read.
        self.value("FIN_RUNWAY", "9", tier=3, justification="")
        finding = self.findings().get("derived_without_workings")
        self.assertIsNotNone(finding)
        self.assertIn("B.7", finding["message"])

    def test_a_derived_figure_showing_its_working_is_not_reported(self):
        self.value("FIN_RUNWAY", "9", tier=3,
                   justification="Closing cash 4.5Cr over net burn 0.5Cr/mo.")
        self.assertNotIn("derived_without_workings", self.findings())

    def test_a_value_with_no_tier_at_all_is_an_error(self):
        """Unreachable through extraction, so a hit is a defect not a gap."""
        pv = self.value("TEAM_FDR_EXP", "12")
        pv.source_tier = None
        pv.source_type = ""
        pv.save()
        finding = self.findings().get("untiered_value")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "error")
        self.assertEqual(self.assessment.audit_status, "failed")


class JudgementTests(IntegrityBase):
    """Anchor rows have no number to contradict them."""

    def test_a_judgement_above_fair_with_nothing_behind_it_is_reported(self):
        self.value("ANC_FDR_EDU", "Top-tier institution", band="Excellent",
                   detail="", justification="")
        finding = self.findings().get("unevidenced_judgement")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "warning")

    def test_a_judgement_with_a_written_reason_is_not_reported(self):
        self.value("ANC_FDR_EDU", "Top-tier institution", band="Excellent",
                   detail="", justification="Both founders are IIT Bombay.")
        self.assertNotIn("unevidenced_judgement", self.findings())

    def test_a_judgement_at_fair_is_not_reported(self):
        """Only generosity is suspicious. Poor and Fair need no defence."""
        self.value("ANC_FDR_EDU", "Regional college", band="Fair",
                   detail="", justification="")
        self.assertNotIn("unevidenced_judgement", self.findings())


class WeightTests(IntegrityBase):
    """What the doubted rows are actually worth once blanks redistribute."""

    def test_effective_weights_sum_to_the_whole_score(self):
        from fundos.assessment import integrity
        from fundos.assessment.services import run_scoring

        for key, raw in (("TEAM_FDR_EXP", "12"), ("TEAM_COFDR_YRS", "5"),
                         ("TEAM_INST_INV", "2"), ("FIN_RUNWAY", "9")):
            self.value(key, raw)
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

        ctx = integrity._Context(self.assessment, None,
                                 self.assessment.tenant_id)
        total = sum(ctx.effective_weights().values())
        # The shares are what the engine actually applied, so they account for
        # the entire score — not for the declared weight of the answered rows.
        self.assertAlmostEqual(total, 1.0, places=6)

    def test_a_doubted_value_carrying_real_weight_is_reported(self):
        # Sole answered row in its category: it carries far more than the 3%
        # floor once its blank siblings drop out of the roll-up.
        self.value("FIN_RUNWAY", "9", confidence="0.20")
        finding = self.findings().get("fragile_weight")
        self.assertIsNotNone(finding)
        self.assertIn("B.7", finding["message"])

    def test_a_confident_value_carrying_the_same_weight_is_not_reported(self):
        self.value("FIN_RUNWAY", "9", confidence="0.95")
        self.assertNotIn("fragile_weight", self.findings())

    def test_weight_resting_on_confidence_is_reported_above_the_ceiling(self):
        # Every answered row is tier 3 => Estimate, so the whole score rests
        # on the extractor's certainty rather than on cited provenance.
        for key, raw in (("TEAM_FDR_EXP", "12"), ("FIN_RUNWAY", "9")):
            self.value(key, raw, tier=3,
                       justification="Derived from the model.")
        finding = self.findings().get("confidence_carried_weight")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "warning")

    def test_verified_evidence_does_not_trip_the_confidence_ceiling(self):
        for key, raw in (("TEAM_FDR_EXP", "12"), ("FIN_RUNWAY", "9")):
            self.value(key, raw, tier=1)
        self.assertNotIn("confidence_carried_weight", self.findings())


class MarketSanityTests(IntegrityBase):
    """A niche cannot be larger than the market containing it."""

    def test_a_sub_sector_tam_above_its_sector_is_an_error(self):
        self.value("SEC_TAM", "10")
        self.value("SEC_TAM_SUB", "50")
        finding = self.findings().get("tam_inverted")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "error")
        # Carried structurally so the row can be shown against the finding.
        self.assertEqual(sorted(finding["parameters"]),
                         ["SEC_TAM", "SEC_TAM_SUB"])

    def test_identical_tam_figures_are_a_warning_not_an_error(self):
        self.value("SEC_TAM", "10")
        self.value("SEC_TAM_SUB", "10")
        finding = self.findings().get("tam_identical")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "warning")

    def test_a_sub_sector_inside_its_sector_is_not_reported(self):
        self.value("SEC_TAM", "50")
        self.value("SEC_TAM_SUB", "10")
        found = self.findings()
        self.assertNotIn("tam_inverted", found)
        self.assertNotIn("tam_identical", found)

    def test_one_missing_tam_figure_reports_nothing(self):
        """Not testable is not a finding about the company."""
        self.value("SEC_TAM", "50")
        found = self.findings()
        self.assertNotIn("tam_inverted", found)
        self.assertNotIn("tam_identical", found)


class CohortTests(IntegrityBase):
    """The deal database, and what happens when a company does not reach it."""

    def test_nothing_is_reported_when_no_deal_database_is_loaded(self):
        """An empty benchmark table is a deployment state, not a finding.

        Reporting it would put the same two warnings on every company in the
        system forever, which is how an audit panel stops being read.
        """
        self.value("TEAM_FDR_EXP", "12")
        found = self.findings()
        self.assertNotIn("sector_unresolved", found)
        self.assertNotIn("sub_sector_unresolved", found)

    def test_an_unresolved_sector_is_reported_once_the_database_exists(self):
        self.sector_row(name="Something Else")
        self.value("TEAM_FDR_EXP", "12")
        finding = self.findings().get("sector_unresolved")
        self.assertIsNotNone(finding)
        self.assertEqual(finding["severity"], "warning")

    def test_a_resolved_sector_is_not_reported(self):
        self.sector_row(name="Ecommerce")
        self.value("TEAM_FDR_EXP", "12")
        self.assertNotIn("sector_unresolved", self.findings())

    def test_blank_database_rows_are_reported(self):
        self.sector_row(name="Ecommerce")
        self.value("TEAM_FDR_EXP", "12")
        finding = self.findings().get("cohort_rows_blank")
        self.assertIsNotNone(finding)
        # All six percentile rows, none of them answered.
        self.assertIn("6 of 6", finding["message"])


class AskContextTests(IntegrityBase):
    """The round size, against what this cohort actually raises."""

    def test_an_ask_far_above_the_cohort_average_is_reported(self):
        self.sector_row(avg_ticket_usd_mn=Decimal("2"))
        self.assessment.capital_raised_usd_mn = Decimal("25")
        self.assessment.save()
        self.value("TEAM_FDR_EXP", "12")
        finding = self.findings().get("ask_outside_cohort")
        self.assertIsNotNone(finding)
        self.assertIn("12.5x", finding["message"])
        self.assertIn("above", finding["message"])

    def test_an_ask_inside_the_cohort_range_is_context_not_a_warning(self):
        self.sector_row(avg_ticket_usd_mn=Decimal("5"))
        self.assessment.capital_raised_usd_mn = Decimal("6")
        self.assessment.save()
        self.value("TEAM_FDR_EXP", "12")
        found = self.findings()
        self.assertNotIn("ask_outside_cohort", found)
        self.assertEqual(found["ask_in_context"]["severity"], "info")

    def test_no_ask_recorded_reports_nothing(self):
        self.sector_row(avg_ticket_usd_mn=Decimal("5"))
        self.value("TEAM_FDR_EXP", "12")
        found = self.findings()
        self.assertNotIn("ask_outside_cohort", found)
        self.assertNotIn("ask_in_context", found)


class NeverRaisesTests(IntegrityBase):
    """A diagnostic that can take down scoring is worse than no diagnostic."""

    def test_a_broken_check_does_not_cost_the_others_their_output(self):
        from unittest import mock

        from fundos.assessment import integrity

        def explode(_ctx):
            raise RuntimeError("boom")

        self.value("SEC_TAM", "10")
        self.value("SEC_TAM_SUB", "50")
        with mock.patch.object(integrity, "_uncited_values", explode):
            found = self.findings()
        # The failing check is absent; the ones around it still reported.
        self.assertIn("tam_inverted", found)
        self.assertNotIn("uncited_value", found)

    def test_scoring_survives_a_context_that_cannot_be_built(self):
        """Losing the audit panel is bad; losing the score is worse."""
        from unittest import mock

        from fundos.assessment import integrity
        from fundos.assessment.services import run_scoring

        def explode(*_a, **_kw):
            raise RuntimeError("boom")

        self.value("TEAM_FDR_EXP", "12")
        with mock.patch.object(integrity, "_Context", explode):
            run_scoring(self.assessment)
        self.assessment.refresh_from_db()

        # A score was still produced, and the block simply contributed
        # nothing rather than taking scoring down with it.
        self.assertIsNotNone(self.assessment.overall_score)
        self.assertFalse([f for f in self.assessment.audit_findings
                          if f.get("source") == "integrity"])


class RatingGapTests(TestCase):
    """Turning the rating label back into a distance."""

    def test_the_gap_names_the_rung_above_and_what_it_costs(self):
        from fundos.assessment.phase1 import rating_gap

        gap = rating_gap(Decimal("6.92"), "Average")
        self.assertEqual(gap["rating"], "Average")
        self.assertEqual(gap["nextRating"], "Good")
        self.assertEqual(gap["nextRatingAt"], 7.0)
        self.assertAlmostEqual(gap["pointsToNext"], 0.08, places=2)

    def test_the_gap_also_names_what_would_lose_the_current_rating(self):
        from fundos.assessment.phase1 import rating_gap

        gap = rating_gap(Decimal("6.92"), "Average")
        self.assertEqual(gap["heldAt"], 6.0)
        self.assertAlmostEqual(gap["pointsToLose"], 0.92, places=2)

    def test_the_top_of_the_ladder_has_no_rung_above_it(self):
        from fundos.assessment.phase1 import rating_gap

        gap = rating_gap(Decimal("9.5"), "Excellent")
        self.assertIsNone(gap["nextRating"])
        self.assertIsNone(gap["pointsToNext"])

    def test_below_the_ladder_the_deal_is_holding_nothing(self):
        from fundos.assessment.phase1 import rating_gap

        gap = rating_gap(Decimal("3.0"), "Not Recommended")
        self.assertEqual(gap["rating"], "Not Recommended")
        self.assertEqual(gap["nextRating"], "Challenging")
        self.assertIsNone(gap["heldAt"])
        self.assertIsNone(gap["pointsToLose"])

    def test_an_unscored_deal_has_no_gap(self):
        from fundos.assessment.phase1 import rating_gap

        self.assertIsNone(rating_gap(None, ""))

    def test_the_ladder_is_the_engines_own(self):
        """The gap must never describe a ladder the rating did not come from."""
        from fundos.assessment.phase1 import rating_gap
        from fundos.engines.deal_assessment import (RATING_LADDER,
                                                    rating_for_score)

        for cut, label in RATING_LADDER:
            gap = rating_gap(cut, rating_for_score(cut))
            self.assertEqual(gap["rating"], label)
            # Exactly on a cut-point: the rung is held by nothing to spare.
            self.assertEqual(gap["pointsToLose"], 0.0)
