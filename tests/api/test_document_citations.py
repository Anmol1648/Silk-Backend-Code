"""The uploaded documents are credited for what they say.

A live run cited web research 143 times and its uploaded documents twice,
though the founders' names were on slide 20 of the deck. Web research is
written as quotable answers; a name in slide text is not, so the model took
the easy source.
"""
from django.test import SimpleTestCase

from fundos.profile import schema
from fundos.profile.pipeline.document_citations import credit_documents

DECK = "Project Orah Teaser_vff.pptx"
MODEL = "Project Orah_Financial Model_vf.xlsx"

DOSSIER = f"""# Consolidated research dossier: Zyla Health

## Founders named by the company (user-provided, unverified - RESEARCH LEADS)
- Khushboo Aggarwal

# Source 2: Company Documents

## Company Presentation

### {DECK}

#### Extracted content

<!-- Slide number: 3 -->
Jayant, a young man working in Gurgaon, lost his life to a cardiac arrest.

### Chart

<!-- Slide number: 20 -->
Khushboo Aggarwal
Aishwary Bhashkar
CEO, Founder
CTO, Co-Founder
17 years experience. Previously led investments at Lightrock.

## Financial Model

### {MODEL}

#### Extracted content

## Transaction Comps
|  | Neuberg Diagnostics | 2025-01-01 | Pre-Series A Funding |
|  | Total Direct Cost | Rs. Lakhs | 129.69 | 16.5 |
| Lives covered | 1,20,000 |

# Source 1: Web Research (search-grounded)

## Batch 2: Founders & Leadership

Khushboo Aggarwal is the founder of Zyla Health. Tanmay Patil is a co-founder.
"""

LABELS = [DECK, MODEL]


def _profile(**sections):
    return {"sections": {key: {"isComplete": True, "data": data,
                               "sources": dict(sources)}
                         for key, (data, sources) in sections.items()}}


class ADocumentThatStatesAValueIsCredited(SimpleTestCase):

    def test_a_founder_named_on_a_slide_cites_that_slide(self):
        web = {"source": "Batch 2: Founders & Leadership", "quote": "x"}
        profile = _profile(founders=(
            [{"name": "Khushboo Aggarwal", "role": "CEO"}], {"0": web}))
        added = credit_documents(profile, DOSSIER, LABELS)
        sources = profile["sections"]["founders"]["sources"]
        self.assertEqual(added, 1)
        self.assertEqual(sources["0"], web)
        self.assertEqual(sources["0.1"]["source"], DECK)
        self.assertEqual(sources["0.1"]["locator"], "Slide 20")
        self.assertEqual(sources["0.1"]["quote"], "Khushboo Aggarwal")

    def test_an_uncited_value_gets_the_document_as_its_citation(self):
        profile = _profile(company_metrics=({"lives_covered": "1,20,000"}, {}))
        credit_documents(profile, DOSSIER, LABELS)
        citation = profile["sections"]["company_metrics"]["sources"][
            "lives_covered"]
        self.assertEqual(citation["source"], MODEL)
        self.assertEqual(citation["locator"], "Transaction Comps")
        self.assertNotIn("|", citation["quote"])

    def test_a_heading_inside_a_deck_does_not_end_the_deck(self):
        """A converted deck carries its own `### Chart` headings."""
        profile = _profile(founders=([{"name": "Khushboo Aggarwal"}], {}))
        credit_documents(profile, DOSSIER, LABELS)
        self.assertEqual(
            profile["sections"]["founders"]["sources"]["0"]["source"], DECK)

    def test_an_existing_document_citation_is_not_duplicated(self):
        deck = {"source": DECK, "locator": "Slide 20", "quote": "Khushboo"}
        profile = _profile(founders=([{"name": "Khushboo Aggarwal"}],
                                     {"0": deck}))
        self.assertEqual(credit_documents(profile, DOSSIER, LABELS), 0)


class NothingIsCreditedByCoincidence(SimpleTestCase):

    def _added(self, data):
        profile = _profile(section=(data, {}))
        credit_documents(profile, DOSSIER, LABELS)
        return profile["sections"]["section"]["sources"]

    def test_a_role_beside_somebody_else_is_not_a_match(self):
        """"Co-Founder" is on the slide -- beside a different person."""
        self.assertEqual(self._added(
            [{"name": "Tanmay Patil", "role": "CTO, Co-Founder"}]), {})

    def test_a_single_common_word(self):
        self.assertEqual(self._added({"city": "Gurgaon"}), {})

    def test_a_short_number(self):
        self.assertEqual(self._added({"revenue": "16.5"}), {})

    def test_a_year(self):
        self.assertEqual(self._added({"founded": "2025"}), {})

    def test_generic_words_only(self):
        self.assertEqual(self._added({"stage": "Pre-Series A"}), {})

    def test_long_narrative(self):
        self.assertEqual(self._added(
            {"description": "Khushboo Aggarwal " * 10}), {})

    def test_web_research_text_is_never_a_document(self):
        self.assertEqual(self._added([{"name": "Tanmay Patil"}]), {})

    def test_the_founder_leads_block_is_not_a_document(self):
        profile = _profile(founders=([{"name": "Khushboo Aggarwal"}], {}))
        credit_documents(profile, DOSSIER.replace("Khushboo Aggarwal\nAishwary",
                                                  "Aishwary"), LABELS)
        self.assertEqual(profile["sections"]["founders"]["sources"], {})


class ItIsSafe(SimpleTestCase):

    def test_no_documents_uploaded(self):
        profile = _profile(founders=([{"name": "Khushboo Aggarwal"}], {}))
        self.assertEqual(credit_documents(profile, DOSSIER, []), 0)

    def test_it_never_raises(self):
        for bad in ({}, {"sections": None}, {"sections": {"x": "y"}}):
            self.assertEqual(credit_documents(bad, DOSSIER, LABELS), 0)

    def test_an_incomplete_section_is_left_alone(self):
        profile = {"sections": {"founders": {
            "isComplete": False, "data": [{"name": "Khushboo Aggarwal"}],
            "sources": {}}}}
        credit_documents(profile, DOSSIER, LABELS)
        self.assertEqual(profile["sections"]["founders"]["sources"], {})


class TheModelIsToldToCheckDocumentsFirst(SimpleTestCase):

    def test_the_instruction_is_in_the_prompt(self):
        block = schema.sources_block(LABELS + ["Batch 2: Founders"],
                                     document_count=2)
        self.assertIn("CHECK THE COMPANY'S UPLOADED FILES FIRST", block)
        self.assertIn("Cite web", block)
