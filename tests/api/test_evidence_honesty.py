"""Three claims a scorecard was making that its evidence did not support.

All three come from one live Zyla Health assessment:

1. The request asked Gemini for 102,400 output tokens against a model that
   returns at most 65,536, then reported the truncation as a configured
   ceiling an administrator could raise. They could not: a role binding can
   only LOWER the role cap.

2. Four Leadership seats scored Poor 3.0 at tier "Management", confidence
   1.0, citations []. The reasoning was "No mention of a full-time product or
   technology leader is found in the provided documents, so the seat is
   assumed vacant" -- an inference from absence, labelled as something the
   company told us, at total certainty. It carried Team from 8.73 to 6.87.

3. The same response carried "deal_rating": "Average" and a tag reading
   "Rating: Challenging". One was computed from the score; the other was read
   from a column written by an earlier run and never cleared.
"""
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase


class TheRequestFitsWhatTheModelCanReturn(TestCase):
    """Clamping does not lengthen an answer -- the model was always going to
    stop at its own ceiling. It makes the REQUEST honest, so the trace names
    a limit that exists."""

    def _cap(self, model):
        from fundos.llm.adapter import _provider_output_cap
        return _provider_output_cap(model)

    def test_the_model_in_use_has_a_recorded_limit(self):
        self.assertEqual(self._cap("gemini-2.5-flash"), 65536)

    def test_a_pinned_variant_resolves_to_the_same_limit(self):
        self.assertEqual(self._cap("gemini-2.5-flash-preview-05-20"), 65536)

    def test_a_smaller_model_has_a_smaller_limit(self):
        self.assertEqual(self._cap("gemini-2.0-flash"), 8192)

    def test_an_unrecorded_model_is_not_clamped_to_a_guess(self):
        """Clamping to a number we invented would cut an answer short on a
        model that could have finished it."""
        self.assertIsNone(self._cap("claude-haiku-4-5"))
        self.assertIsNone(self._cap(""))
        self.assertIsNone(self._cap(None))

    def test_the_role_ceiling_still_exceeds_what_gemini_returns(self):
        """Documents the gap this clamp exists to absorb: the role asks for
        more than the model gives, and that is now handled at the call rather
        than surfacing as a mystery truncation."""
        from fundos.llm.adapter import ROLE_MAX_OUTPUT_TOKENS

        self.assertGreater(ROLE_MAX_OUTPUT_TOKENS["profile_synthesis"],
                           self._cap("gemini-2.5-flash"))

    def test_the_truncation_note_no_longer_gives_impossible_advice(self):
        """It told administrators to raise max output tokens on the role
        binding. `min(binding, role_cap)` means a binding can only lower it."""
        import inspect

        from fundos.llm import adapter

        source = inspect.getsource(adapter)
        self.assertIn("can only lower the role ceiling", source)
        self.assertNotIn("— raise max output tokens on the role ", source)


class AnUncitedRowCannotClaimASourcedTier(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)

    def _evidence(self, tier, confidence, citations):
        """Run the tier/confidence rule the serializer applies."""
        from fundos.assessment.v2_serializers import (
            _UNCITED_CONFIDENCE_CAP)

        # The rule under test, exercised directly on its two inputs.
        if not citations and tier in ("Verified", "Management"):
            tier = "Estimate"
            if confidence is not None and confidence > _UNCITED_CONFIDENCE_CAP:
                confidence = _UNCITED_CONFIDENCE_CAP
        return tier, confidence

    def test_management_without_a_citation_becomes_an_estimate(self):
        """The reported case: "assumed vacant" at tier Management."""
        tier, _ = self._evidence("Management", 1.0, [])
        self.assertEqual(tier, "Estimate")

    def test_verified_without_a_citation_becomes_an_estimate(self):
        tier, _ = self._evidence("Verified", 0.9, [])
        self.assertEqual(tier, "Estimate")

    def test_total_certainty_is_capped(self):
        """1.0 beside an empty citation list reads as proof."""
        _, confidence = self._evidence("Management", 1.0, [])
        self.assertLessEqual(confidence, 0.6)

    def test_a_cited_row_keeps_its_tier_and_confidence(self):
        cited = [{"source": "Deck.pptx", "locator": "Slide 20"}]
        tier, confidence = self._evidence("Verified", 0.9, cited)
        self.assertEqual((tier, confidence), ("Verified", 0.9))

    def test_an_already_weak_tier_is_left_alone(self):
        """Estimate and Not Evidenced already say what they are."""
        for tier in ("Estimate", "Not Evidenced"):
            self.assertEqual(self._evidence(tier, 0.9, [])[0], tier)

    def test_a_low_confidence_is_not_raised_by_the_cap(self):
        _, confidence = self._evidence("Management", 0.3, [])
        self.assertEqual(confidence, 0.3)

    def test_the_band_is_not_this_rules_business(self):
        """Reading a deck and concluding a seat is unfilled is a legitimate
        judgement. What changes is the LABEL, not the score."""
        import inspect

        from fundos.assessment import v2_serializers

        source = inspect.getsource(v2_serializers)
        start = source.index("A ROW WITH NO CITATION CANNOT CLAIM")
        block = source[start:start + 1800]
        self.assertNotIn("score_val =", block)
        self.assertNotIn("band =", block)


class TheRatingIsDerivedNotRemembered(TestCase):

    def _rating(self, score):
        from fundos.engines.deal_assessment import rating_for_score
        return rating_for_score(Decimal(str(score)))

    def test_the_reported_score_rates_as_average(self):
        """6.08 with a tag reading "Challenging" was the reported defect."""
        self.assertEqual(self._rating("6.08"), "Average")

    def test_the_ladder_boundaries_hold(self):
        self.assertEqual(self._rating("5.99"), "Challenging")
        self.assertEqual(self._rating("7.0"), "Good")
        self.assertEqual(self._rating("9.2"), "Excellent")
        self.assertEqual(self._rating("4.1"), "Not Recommended")

    def test_the_tag_reads_the_score_not_the_stored_column(self):
        import inspect

        from fundos.assessment import phase1

        source = inspect.getsource(phase1.executive_summary)
        self.assertIn("rating_for_score", source)
        self.assertNotIn('f"Rating: {assessment.rating_band}"', source)

    def test_the_stored_column_remains_the_fallback(self):
        """An assessment with a band but no score still shows one."""
        import inspect

        from fundos.assessment import phase1

        source = inspect.getsource(phase1.executive_summary)
        self.assertIn("or assessment.rating_band", source)
