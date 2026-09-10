"""Two quality checks the reference implementation runs and this one did not.

A perfect score has to be a human verdict, and an absence has to be named
rather than merely counted.
"""
from decimal import Decimal

from tests.api.test_fundraising_phase1 import Phase1Base


class APerfectScoreIsAHumanVerdict(Phase1Base):
    """The bands stop at 9 and the scale runs to 10. The gap is deliberate."""

    def _findings(self, code):
        from fundos.assessment import integrity
        return [f for f in integrity.checks(self.assessment)
                if f["check"] == code]

    def _set(self, **defaults):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_FDR_EXP",
            defaults={"raw_value": "15", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9"), **defaults})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def test_a_clean_assessment_reports_nothing(self):
        self._set()
        self.assertFalse(self._findings("machine_perfect_score"))
        self.assertFalse(self._findings("perfect_score_awarded"))

    def test_a_ten_with_no_override_is_an_error(self):
        """Not a finding about the company — the guard did not run."""
        self._set()
        from fundos.assessment.models import ParameterValue
        ParameterValue.objects.filter(
            assessment=self.assessment, input_key="TEAM_FDR_EXP").update(
                score=Decimal("10"), band="Exceptional", is_overridden=False)

        findings = self._findings("machine_perfect_score")
        self.assertTrue(findings)
        self.assertEqual(findings[0]["severity"], "error")
        self.assertIn("TEAM_FDR_EXP", findings[0]["parameters"])

    def test_a_reviewer_awarded_ten_is_reported_not_faulted(self):
        self._set()
        from fundos.assessment.models import ParameterValue
        ParameterValue.objects.filter(
            assessment=self.assessment, input_key="TEAM_FDR_EXP").update(
                score=Decimal("10"), band="Exceptional", is_overridden=True,
                override_comment="Two prior exits in this exact segment.")

        self.assertFalse(self._findings("machine_perfect_score"))
        findings = self._findings("perfect_score_awarded")
        self.assertTrue(findings)
        self.assertEqual(findings[0]["severity"], "info")

    def test_an_awarded_ten_with_no_reason_is_a_warning(self):
        """The band exists so a reviewer can say why. Without the why it cannot
        be defended to anyone who asks."""
        self._set()
        from fundos.assessment.models import ParameterValue
        ParameterValue.objects.filter(
            assessment=self.assessment, input_key="TEAM_FDR_EXP").update(
                score=Decimal("10"), band="Exceptional", is_overridden=True,
                override_comment="")

        findings = self._findings("perfect_score_uncommented")
        self.assertTrue(findings)
        self.assertEqual(findings[0]["severity"], "warning")

    def test_a_nine_is_not_flagged(self):
        """Excellent is the top the automated path may reach, and does."""
        self._set()
        pv = self.assessment.parameter_values.get(input_key="TEAM_FDR_EXP")
        self.assertEqual(float(pv.score), 9.0)
        self.assertFalse(self._findings("machine_perfect_score"))


class AGapIsNamedNotOnlyCounted(Phase1Base):
    """The counterweight to 'blank is not zero'."""

    def _finding(self):
        from fundos.assessment import integrity
        found = [f for f in integrity.checks(self.assessment)
                 if f["check"] == "complete_pack_gaps"]
        return found[0] if found else None

    def test_an_unevidenced_row_is_named_with_what_to_ask_for(self):
        finding = self._finding()
        self.assertIsNotNone(finding, "a mostly-blank deal reports no gaps")
        # The workbook's own if_missing line, so the ask is specific.
        self.assertIn("—", finding["message"])
        self.assertTrue(finding["parameters"])

    def test_the_message_says_what_the_gaps_were_worth(self):
        """Six blank rows worth 2% is a different deal from two worth 30%."""
        self.assertIn("% of the rating", self._finding()["message"])

    def test_reference_rows_are_not_listed_as_gaps(self):
        """They do not score by design; listing them buries the real ones."""
        from fundos.assessment.models import ConfigParameter
        finding = self._finding()
        refs = set(ConfigParameter.objects.filter(
            is_active=True, feeds_score=False).values_list("input_key",
                                                           flat=True))
        self.assertFalse(refs & set(finding["parameters"]))

    def test_a_fully_evidenced_deal_reports_nothing(self):
        from fundos.assessment.models import ConfigParameter, ParameterValue
        from fundos.assessment.services import run_scoring

        for cfg in ConfigParameter.objects.filter(is_active=True,
                                                  feeds_score=True):
            defaults = {"category": cfg.category_code, "raw_value": "5",
                        "source_type": "document", "source_tier": 1,
                        "confidence": Decimal("0.9")}
            if cfg.scoring_type == "anchor":
                defaults["band"] = "Good"
                defaults["raw_value"] = ""
            ParameterValue.objects.update_or_create(
                assessment=self.assessment, input_key=cfg.input_key,
                defaults=defaults)
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

        finding = self._finding()
        if finding is not None:
            # Whatever is left must genuinely have failed to score, not merely
            # have been unanswered.
            for key in finding["parameters"]:
                pv = self.assessment.parameter_values.filter(
                    input_key=key).first()
                self.assertTrue(pv is None or pv.score is None, key)
