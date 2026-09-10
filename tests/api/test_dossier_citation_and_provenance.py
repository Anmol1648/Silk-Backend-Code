"""Evidence read from the dossier must be kept, and its origin must show.

A 27 Aug run answered ~36 parameters and discarded TWENTY-TWO of them for
lacking a citation — every qualitative anchor among them — so Financials,
Deal Dynamics and Business Quality rendered blank on a company whose
845,000-character dossier was in the prompt. The model had read the evidence;
it simply had no filename to quote, because a merged corpus has none.

Losing evidence we hold is not caution. The dossier is stored per run and can
be re-read, so it is a checkable source — credited at a lower tier so a named
file or URL still wins wherever both exist.
"""
from django.test import TestCase

from fundos.assessment.phase1 import TIER_MEANING, provenance
from fundos.profile.assessment_extraction import (DOSSIER_SOURCE, DOSSIER_TIER,
                                                  _citation)


class DossierCitationTests(TestCase):

    def test_an_uncited_value_is_kept_when_a_dossier_was_supplied(self):
        _, detail, ok = _citation({"value": "12"}, dossier=True)
        self.assertTrue(ok)
        self.assertEqual(detail, DOSSIER_SOURCE)

    def test_it_is_still_rejected_without_a_dossier(self):
        """No evidence in context means an uncited answer is recollection."""
        _, _, ok = _citation({"value": "12"}, dossier=False)
        self.assertFalse(ok)

    def test_a_named_source_is_preferred_over_the_dossier_fallback(self):
        _, detail, _ = _citation({"sourceDoc": "Model.xlsx"}, dossier=True)
        self.assertEqual(detail, "Model.xlsx")
        url, _, _ = _citation({"sourceUrl": "https://a.test/x"}, dossier=True)
        self.assertEqual(url, "https://a.test/x")

    def test_the_dossier_tier_ranks_below_a_named_source(self):
        """Precedence is decided by tier in profile_bridge._outranks."""
        self.assertGreater(DOSSIER_TIER, 1,
                           "a merged corpus must not outrank a named file")

    def test_the_fallback_is_off_by_default(self):
        """Callers must opt in, so no existing path loosens silently."""
        _, _, ok = _citation({"value": "12"})
        self.assertFalse(ok)


class ProvenanceTests(TestCase):
    """The drawer must say where a figure came from, not just what it is."""

    class _PV:
        def __init__(self, **kw):
            self.source_detail = kw.get("detail", "")
            self.source_type = kw.get("kind", "document")
            self.source_tier = kw.get("tier", 1)
            self.confidence = kw.get("confidence", 0.9)
            self.is_overridden = kw.get("overridden", False)
            self.override_comment = kw.get("comment", "")
            self.system_band = kw.get("system_band", "Good")

    class _Cfg:
        def __init__(self, scoring_type="rubric"):
            self.scoring_type = scoring_type

    def test_an_unanswered_parameter_says_so_without_inventing(self):
        p = provenance(None, self._Cfg())
        self.assertIn("excluded from the score", p["statement"])
        self.assertIsNone(p["tier"])

    def test_the_source_and_tier_are_reported(self):
        p = provenance(self._PV(detail="Model.xlsx, Summary tab", tier=1),
                       self._Cfg())
        self.assertEqual(p["source"], "Model.xlsx, Summary tab")
        self.assertEqual(p["tier"], 1)
        self.assertEqual(p["tierMeaning"], TIER_MEANING[1])

    def test_a_url_is_split_out_so_it_can_be_linked(self):
        p = provenance(
            self._PV(detail="https://zyla.in/about — company site"),
            self._Cfg())
        self.assertEqual(p["sourceUrl"], "https://zyla.in/about")

    def test_the_scoring_method_is_explained(self):
        self.assertIn("cut-points",
                      provenance(self._PV(), self._Cfg("rubric"))["method"])
        self.assertIn("anchor definitions",
                      provenance(self._PV(), self._Cfg("anchor"))["method"])
        self.assertIn("percentile",
                      provenance(self._PV(), self._Cfg("lookup"))["method"])

    def test_a_lookup_row_says_the_company_cannot_move_it(self):
        p = provenance(self._PV(kind="benchmark"), self._Cfg("lookup"))
        self.assertIn("cannot move this row", p["statement"])

    def test_an_override_names_the_reviewer_reason_and_keeps_the_system_band(self):
        p = provenance(
            self._PV(overridden=True, comment="Sector experience is thin.",
                     system_band="Excellent"),
            self._Cfg())
        self.assertIn("reviewer set this score by hand", p["statement"])
        self.assertIn("Sector experience is thin.", p["statement"])
        self.assertIn("Excellent", p["statement"],
                      "the rule-based result must remain visible")

    def test_a_missing_source_is_reported_not_fabricated(self):
        p = provenance(self._PV(detail=""), self._Cfg())
        self.assertEqual(p["source"], "Not recorded")

    def test_provenance_is_on_every_scorecard_leaf(self):
        from fundos.assessment import phase1
        import inspect
        src = inspect.getsource(phase1._leaf)
        self.assertIn('"provenance"', src)
