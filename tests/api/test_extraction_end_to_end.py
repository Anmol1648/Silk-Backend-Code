"""Document extraction, all the way through to a score.

The unit tests either side of this one check the prompt and the validators.
This checks the thing that actually broke: that a band reported for an
anchor-scored parameter survives extraction, reaches `pv.band`, and is banded
and scored by the engine -- because every link in that chain existed and the
first one was missing, so all 24 anchor rows scored blank on every company
this system has ever assessed.
"""
from decimal import Decimal
from unittest import mock

from tests.api.test_fundraising_phase1 import Phase1Base


class TheModelsAnswerReachesTheScore(Phase1Base):

    def setUp(self):
        super().setUp()
        from django.core.management import call_command
        # The prompt is built from the dictionary prose, which the config seed
        # does not ship.
        call_command("seed_parameter_dictionary", verbosity=0)
        # The base fixture seeds tier-1 values for the whole scorecard. They
        # are exactly what `merge_value` is built to protect, so extraction
        # correctly declines to overwrite them -- and this test is about what
        # extraction WRITES. Cleared so the assessment starts empty.
        self.assessment.parameter_values.all().delete()

    def _run(self, values):
        """Run extraction with a stubbed model answer and return the counts."""
        from fundos.assessment import extraction

        self.prompts = []

        def fake_generate(**kwargs):
            prompt = kwargs.get("prompt") or ""
            self.prompts.append(prompt)
            # The question set is chunked, and a key appears in exactly one
            # chunk. Answering every chunk with the same rows would count each
            # one as many times as there are batches.
            return {"values": [v for v in values
                               if ('"%s"' % v["inputKey"]) in prompt]}

        with mock.patch("fundos.llm.adapter.llm_generate", fake_generate), \
                mock.patch.object(extraction, "_deal_documents",
                                  return_value=[{"name": "deck.pptx",
                                                 "storage_uri": "x://deck",
                                                 "id": "1"}]), \
                mock.patch.object(extraction, "_fetch", return_value="/tmp/x"), \
                mock.patch.object(extraction, "read_document",
                                  return_value=("Founder holds an MBA from "
                                                "IIM-K. 15 years in health.",
                                                "pptx")):
            counts = extraction.extract_for_assessment(self.assessment)

        from fundos.assessment.services import run_scoring
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()
        return counts

    def _pv(self, key):
        return self.assessment.parameter_values.filter(input_key=key).first()

    def test_an_anchor_band_is_stored_and_scored(self):
        """The whole point. This row could not score at all before."""
        self._run([{
            "inputKey": "ANC_FDR_EDU",
            "value": None,
            "band": "Excellent",
            "evidenceTier": "Verified",
            "locator": "deck.pptx, slide 20",
            "quote": "Khushboo Aggarwal, CEO, holds an MBA from IIM-K.",
            "confidence": 0.95,
            "justification": "IIM-K is a tier 1 institution.",
        }])
        pv = self._pv("ANC_FDR_EDU")
        self.assertIsNotNone(pv, "the row was dropped before it was stored")
        self.assertEqual(pv.band, "Excellent")
        self.assertIsNotNone(pv.score, "stored but never scored")
        self.assertEqual(float(pv.score), 9.0)

    def test_a_band_with_no_number_is_no_longer_counted_unanswered(self):
        """The exact defect: the old test looked only at the value."""
        counts = self._run([{
            "inputKey": "ANC_FDR_EDU", "value": None, "band": "Good",
            "evidenceTier": "Verified", "locator": "deck.pptx, slide 20",
            "confidence": 0.9,
        }])
        self.assertEqual(counts["unanswered"], 0)
        self.assertEqual(counts["values"], 1)
        self.assertEqual(counts["bands"], 1)

    def test_exceptional_is_refused_before_it_is_stored(self):
        """A model may not mint a 10/10 with no override record."""
        counts = self._run([{
            "inputKey": "ANC_FDR_EDU", "value": None, "band": "Exceptional",
            "evidenceTier": "Verified", "locator": "deck.pptx",
            "confidence": 0.9,
        }])
        self.assertEqual(counts["unanswered"], 1)
        self.assertIsNone(self._pv("ANC_FDR_EDU"))

    def test_a_band_offered_for_a_numeric_row_is_dropped(self):
        """The engine bands those from the published cut-points.

        A model's opinion may not displace a rubric, so the band is discarded
        while the number it reported is kept.
        """
        self._run([{
            "inputKey": "TEAM_FDR_EXP", "value": 15, "band": "Poor",
            "evidenceTier": "Verified", "locator": "deck.pptx, slide 20",
            "confidence": 0.9,
        }])
        pv = self._pv("TEAM_FDR_EXP")
        self.assertEqual(pv.raw_value, "15")
        # 15 years clears the Series A Excellent cut-point of 10.
        self.assertEqual(pv.band, "Excellent")
        self.assertEqual(float(pv.score), 9.0)

    def test_the_reported_tier_is_stored_not_a_constant(self):
        """Every document row used to be written tier 1 regardless."""
        self._run([
            {"inputKey": "TEAM_FDR_EXP", "value": 15,
             "evidenceTier": "Verified", "locator": "deck.pptx", "confidence": 0.9},
            {"inputKey": "TEAM_COFDR_YRS", "value": 25,
             "evidenceTier": "Management", "locator": "deck.pptx", "confidence": 0.8},
        ])
        self.assertEqual(self._pv("TEAM_FDR_EXP").source_tier, 1)
        self.assertEqual(self._pv("TEAM_COFDR_YRS").source_tier, 2)

    def test_a_row_the_model_could_not_establish_is_not_stored(self):
        """Blank stays blank, and the weight redistributes."""
        counts = self._run([{
            "inputKey": "TEAM_FDR_EXP", "value": None,
            "evidenceTier": "Not Evidenced",
            "justification": "No founder bio in the pack.", "confidence": 0.9,
        }])
        self.assertIsNone(self._pv("TEAM_FDR_EXP"))
        self.assertEqual(counts["values"], 0)

    def test_a_derived_figure_is_stored_as_an_estimate(self):
        """Tier 3. It is kept and visible, and the reader sees it was derived."""
        self._run([{
            "inputKey": "TEAM_FDR_EXP", "value": 12, "evidenceTier": "Estimate",
            "locator": "deck.pptx", "confidence": 0.7,
            "justification": "Inferred from the graduation year.",
        }])
        self.assertEqual(self._pv("TEAM_FDR_EXP").source_tier, 3)

    def test_the_quote_is_kept_beside_the_locator(self):
        """A citation a reader can check in place, not just a filename."""
        self._run([{
            "inputKey": "TEAM_FDR_EXP", "value": 15, "evidenceTier": "Verified",
            "locator": "deck.pptx, slide 20",
            "quote": "15 years experience in healthcare",
            "confidence": 0.9,
        }])
        detail = self._pv("TEAM_FDR_EXP").source_detail
        self.assertIn("slide 20", detail)
        self.assertIn("15 years experience", detail)

    def test_the_prompt_carried_the_specification(self):
        """What the model was actually sent, not what we meant to send."""
        self._run([{"inputKey": "TEAM_FDR_EXP", "value": 15,
                    "evidenceTier": "Verified", "locator": "d", "confidence": 0.9}])
        joined = "\n".join(self.prompts)
        self.assertIn("PARAMETERS", joined)
        self.assertIn("Definition:", joined)
        self.assertIn("Where to look:", joined)
        self.assertIn("Series A", joined)
        self.assertIn("DOCUMENTS", joined)
        self.assertIn('Report "band"', joined,
                      "no anchor row was ever asked for a band")


class CoverageRisesWhenAnchorsScore(Phase1Base):
    """24 anchor rows going from never-scoreable to scoreable is coverage."""

    def test_the_anchor_rows_are_a_material_share_of_the_model(self):
        from fundos.assessment.models import ConfigParameter
        anchors = ConfigParameter.objects.filter(
            is_active=True, feeds_score=True, scoring_type="anchor").count()
        scored = ConfigParameter.objects.filter(
            is_active=True, feeds_score=True).count()
        self.assertGreater(anchors, 0)
        # Worth stating in the test rather than the commit message: this is
        # the size of the hole the missing band field left in every
        # assessment this system has produced.
        self.assertGreater(anchors / scored, 0.15,
                           "anchor rows should be a material share")
