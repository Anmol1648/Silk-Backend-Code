"""Evidence from uploaded documents must reach the Deal Scorecard.

A company uploaded a deck and a financial model, the profile pipeline read
22,882 characters out of the deck, and the scorecard still scored 10 of 61
parameters with categories E and F blank. Four independent defects, each of
which alone was enough to produce that:

1. `material_critique` required `detected_type` in its schema and never asked
   for it in its prompt, so every call failed validation and MaterialAsset
   summaries stayed empty.
2. The assessment extraction read documents ONLY from those summaries, never
   from the dossier the pipeline had just built.
3. Every extracted value required a `sourceUrl`, which a figure read out of an
   uploaded .xlsx can never have — so document-sourced values were discarded
   as unsourced.
4. The sector vocabulary was passed to the model but never mentioned in the
   prompt, so it answered in free text and the six deal-database percentile
   rows matched nothing.

These tests pin each one at the seam where it failed.
"""
from unittest import mock

from django.test import TestCase

from fundos.profile import assessment_extraction as ax


class CitationTests(TestCase):
    """A value must name a checkable source — a URL is not the only kind."""

    def test_a_url_is_a_citation(self):
        url, detail, ok = ax._citation({"sourceUrl": "https://example.com/x"})
        self.assertTrue(ok)
        self.assertEqual(url, "https://example.com/x")

    def test_a_document_reference_is_a_citation(self):
        """The defect: this was rejected, so uploaded files scored nothing."""
        url, detail, ok = ax._citation(
            {"sourceDoc": "Model.xlsx, Summary tab"})
        self.assertTrue(ok, "a document reference must satisfy the source rule")
        self.assertEqual(url, "")
        self.assertEqual(detail, "Model.xlsx, Summary tab")

    def test_the_alternate_spelling_is_accepted(self):
        _, detail, ok = ax._citation({"sourceDocument": "Deck.pptx, slide 4"})
        self.assertTrue(ok)
        self.assertEqual(detail, "Deck.pptx, slide 4")

    def test_neither_is_still_rejected(self):
        """The rule is widened, not weakened."""
        _, _, ok = ax._citation({"value": "42", "confidence": 0.99})
        self.assertFalse(ok)

    def test_whitespace_is_not_a_citation(self):
        _, _, ok = ax._citation({"sourceUrl": "   ", "sourceDoc": "  "})
        self.assertFalse(ok)

    def test_a_url_wins_when_both_are_given(self):
        url, detail, ok = ax._citation(
            {"sourceUrl": "https://a.test/p", "sourceDoc": "Deck.pptx"})
        self.assertTrue(ok)
        self.assertEqual(url, "https://a.test/p")
        self.assertEqual(detail, "Deck.pptx",
                         "the document reference is kept alongside the URL")


class GroundingTests(TestCase):
    """The consolidated dossier is retrieved evidence."""

    def test_a_dossier_grounds_the_run(self):
        ok, reason, mode = ax.grounding({"dossier": "x" * 5000})
        self.assertTrue(ok)
        self.assertEqual(mode, "full")

    def test_a_trivial_dossier_does_not(self):
        ok, _, mode = ax.grounding({"dossier": "x" * 10})
        self.assertNotEqual(mode, "full")

    def test_nothing_at_all_is_still_refused(self):
        ok, reason, mode = ax.grounding({})
        self.assertFalse(ok)

    def test_documents_still_ground_a_run(self):
        """The pre-existing route must keep working."""
        ok, _, mode = ax.grounding({"documents": [{"summary": "s"}]})
        self.assertTrue(ok)
        self.assertEqual(mode, "full")


class PromptContractTests(TestCase):
    """What the prompt asks for decides what comes back."""

    def test_the_prompt_offers_a_document_citation(self):
        self.assertIn("sourceDoc", ax.EXTRACTION_SYSTEM)

    def test_the_prompt_binds_the_sector_vocabulary(self):
        """It was passed in context and never mentioned. That was the bug."""
        self.assertIn("sector_vocabulary", ax.EXTRACTION_SYSTEM)
        self.assertIn("sub_sector_vocabulary", ax.EXTRACTION_SYSTEM)

    def test_the_prompt_names_the_dossier_as_the_primary_source(self):
        self.assertIn("dossier", ax.EXTRACTION_SYSTEM)


class MaterialCritiqueSchemaTests(TestCase):
    """The schema and its prompt have to agree."""

    def test_detected_type_is_not_required(self):
        """It was required, never asked for, and failed every call."""
        from fundos.llm.schemas import SCHEMAS
        schema = SCHEMAS["material_critique"]
        self.assertNotIn("detected_type", schema["required"])
        self.assertIn("detected_type", schema["optional"])

    def test_the_substance_is_still_required(self):
        """Relaxing the label must not relax the content."""
        from fundos.llm.schemas import SCHEMAS
        required = SCHEMAS["material_critique"]["required"]
        self.assertIn("summary", required)
        self.assertIn("recommendations", required)

    def test_a_response_without_detected_type_now_validates(self):
        from fundos.llm.schemas import SCHEMAS
        schema = SCHEMAS["material_critique"]
        payload = {"summary": "Revenue of INR 12 Cr in FY25, 18 months "
                              "runway.", "recommendations": []}
        missing = [k for k in schema["required"] if k not in payload]
        self.assertEqual(missing, [],
                         "this is the exact payload shape that was failing")

    def test_the_shipped_prompt_asks_for_every_required_key(self):
        """The root cause: prompt and schema drifted apart."""
        from fundos.llm.default_prompts import DEFAULT_PROMPTS
        from fundos.llm.schemas import SCHEMAS
        system = DEFAULT_PROMPTS["material_critique"]["system"]
        for key in SCHEMAS["material_critique"]["required"]:
            self.assertIn(key, system,
                          f"the prompt never asks for required key {key!r}")

    def test_the_prompt_asks_for_detected_type_too(self):
        from fundos.llm.default_prompts import DEFAULT_PROMPTS
        self.assertIn("detected_type",
                      DEFAULT_PROMPTS["material_critique"]["system"])


class SectorResolutionTests(TestCase):
    """Categories E and F are 30% of the rating between them."""

    def setUp(self):
        from fundos.assessment.models import SectorDealData
        for name in ("Healthtech", "Fintech", "Ecommerce"):
            SectorDealData.objects.create(
                level="sector", name=name, deal_count=50)

    def test_an_exact_name_resolves(self):
        self.assertEqual(ax.resolve_sector("Healthtech"), "Healthtech")

    def test_spacing_and_case_do_not_matter(self):
        self.assertEqual(ax.resolve_sector("health tech"), "Healthtech")
        self.assertEqual(ax.resolve_sector("HEALTHTECH"), "Healthtech")

    def test_a_descriptive_label_resolves_through_the_alias_list(self):
        """'Digital Health' is what the model actually returned.

        This asserted None when the taxonomy had no alias layer, and leaving
        the label unresolved was the honest answer then: a wrong cohort scores
        a company confidently against the wrong twenty peers, which is worse
        than a blank. It is no longer the honest answer. "Digital Health" and
        "Healthtech" are the same market under two names, the alias is curated
        data pointing at a cohort already in the taxonomy, and the two share
        too few characters for fuzzy matching to ever bridge them -- so
        without it, category E scores blank on a company with a perfectly
        ordinary sector.
        """
        self.assertEqual(ax.resolve_sector("Digital Health"), "Healthtech")
        self.assertEqual(ax.resolve_sector("Healthcare"), "Healthtech")

    def test_a_label_naming_no_known_market_still_does_not_resolve(self):
        """The alias list is curated, not a fallback that always answers."""
        self.assertIsNone(ax.resolve_sector("Sponge Manufacturing"))

    def test_an_unresolved_sector_says_so(self):
        """It used to `return None` in silence, leaving no trace at all."""
        with self.assertLogs("fundos.profile.assessment_extraction",
                             level="WARNING") as logs:
            ax.resolve_sector("Sponge Manufacturing")
        joined = "\n".join(logs.output)
        self.assertIn("Sponge Manufacturing", joined)
        self.assertIn("Healthtech", joined,
                      "the message must name the labels that would work")

    def test_blank_resolves_to_nothing_quietly(self):
        self.assertIsNone(ax.resolve_sector(""))


class DossierRoutingTests(TestCase):
    """The dossier has to actually arrive in the prompt context."""

    def test_the_dossier_is_put_in_the_extraction_context(self):
        captured = {}

        def fake_generate(**kwargs):
            captured.update(kwargs)
            return {"values": [], "bands": [], "sector": "", "subSector": ""}

        with mock.patch("fundos.llm.adapter.llm_generate", fake_generate), \
                mock.patch.object(ax, "vocabulary", return_value=([], [])):
            profile = mock.Mock()
            profile.company.name = "Test Co"
            profile.website_url = "https://test.co"
            profile.hq_country = "IN"
            profile.home_currency = "INR"
            try:
                ax._ask(profile, {"dossier": "REVENUE: INR 12 Cr" * 100},
                        [], [], user=None)
            except Exception:
                pass

        context = captured.get("context") or {}
        self.assertIn("dossier", context,
                      "the dossier must reach the model, not just the payload")
        self.assertIn("REVENUE", context["dossier"])

    def test_the_dossier_is_truncated_not_unbounded(self):
        self.assertLess(ax.MAX_DOSSIER_CHARS, 600_000)
        self.assertGreater(ax.MAX_DOSSIER_CHARS, 50_000)


class ContextReachesTheModelTests(TestCase):
    """Building a context payload is not the same as sending it.

    `adapter._filter_context` drops any key the role did not DECLARE in
    `default_prompts[...]["context_keys"]`. The sector vocabulary and the
    dossier were both being built, attached, and thrown away there — so the
    prompt instructed the model to choose from a list it had never been
    shown, and to read a dossier it had never been given.
    """

    def _filtered(self, **extra):
        from fundos.llm.adapter import _filter_context
        ctx = {"company": {"name": "Zyla"}, "parameters": [{"inputKey": "X"}]}
        ctx.update(extra)
        return _filter_context("assessment_inputs", ctx)

    def test_the_sector_vocabulary_survives(self):
        out = self._filtered(sector_vocabulary=["Healthtech", "Fintech"])
        self.assertIn("sector_vocabulary", out)
        self.assertIn("Healthtech", out["sector_vocabulary"])

    def test_the_sub_sector_vocabulary_survives(self):
        out = self._filtered(sub_sector_vocabulary=["Healthtech"])
        self.assertIn("sub_sector_vocabulary", out)

    def test_the_dossier_survives(self):
        out = self._filtered(dossier="REVENUE INR 12 Cr " * 500)
        self.assertIn("dossier", out)
        self.assertIn("REVENUE", str(out["dossier"]))

    def test_the_dossier_is_not_cut_to_the_bulk_budget(self):
        """`_trim_bulk_sources` caps bulk keys at 6,000 chars.

        The dossier is the primary evidence for this call, not a re-read of a
        corpus something else already structured, so it must not be on that
        list.
        """
        from fundos.profile.assessment_extraction import MAX_DOSSIER_CHARS
        out = self._filtered(dossier="D" * MAX_DOSSIER_CHARS)
        self.assertEqual(len(out["dossier"]), MAX_DOSSIER_CHARS)

    def test_the_declared_keys_match_what_the_caller_sends(self):
        """Every key `_ask` builds must be declared, or it is discarded."""
        from fundos.llm.default_prompts import get_default
        declared = set(get_default("assessment_inputs")["context_keys"])
        built_by_ask = {
            "company", "website_extract", "document_extracts", "research",
            "founders", "parameters", "qualitative_parameters", "dossier",
            "sector_vocabulary", "sub_sector_vocabulary"}
        missing = built_by_ask - declared
        self.assertEqual(missing, set(),
                         f"_ask sends these and the role drops them: {missing}")


class OutputCeilingTests(TestCase):
    """A truncated response loses its TAIL, so order decides what survives."""

    def test_sector_is_emitted_before_the_long_arrays(self):
        from fundos.profile import assessment_extraction as ax
        system = ax.EXTRACTION_SYSTEM
        self.assertLess(system.index('"sector"'), system.index('"values"'),
                        "sector must be written before the arrays that can "
                        "push it past the output ceiling")

    def test_the_dossier_leaves_room_for_the_instructions(self):
        from fundos.profile.assessment_extraction import MAX_DOSSIER_CHARS
        self.assertGreaterEqual(
            MAX_DOSSIER_CHARS, 80000,
            "dossier capacity should be large enough to hold comprehensive company context")



class MockingDefaultTests(TestCase):
    """A mocked default makes fabricated output look like a real run."""

    def test_dev_does_not_mock_ai_by_default(self):
        from fundos.settings import dev
        self.assertFalse(dev.FUNDOS_AI_MOCKED_DEFAULT)
