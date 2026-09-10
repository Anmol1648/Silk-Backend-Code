"""Where each value came from — the same evidence the assessment shows.

The endpoint existed and cited nothing. It split `field_id` on a dot while
every caller sends `company_profile__description_of_business`, so the
per-field branch was unreachable on every real request — and nobody noticed,
because the run-level fallback still returned two lines of generalities about
"web research and uploaded documents". That reads like provenance and names
nothing: no document, no slide, no quote.

Provenance is now recorded at synthesis time, because synthesis is the only
party that knows. It has the dossier in front of it and writes the value;
nothing downstream can recover which of forty pages a sentence came from
without guessing, and a guessed citation is worse than none — it survives
review by looking exactly like a real one.
"""
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status

from fundos.profile import schema as profile_schema
from tests.conftest_helpers import auth_headers, make_world

TEASER = "Project Orah Teaser_vff.pptx"
XLSX = "Model.xlsx"


class TheModelIsAskedToCite(TestCase):

    def test_the_prompt_demands_a_verbatim_quote(self):
        block = profile_schema.schema_prompt_block()
        self.assertIn("sources", block)
        self.assertIn("verbatim", block)
        self.assertIn("never a paraphrase", block)

    def test_the_prompt_forbids_inventing_a_citation(self):
        block = profile_schema.schema_prompt_block()
        self.assertIn("An omitted citation is honest", block)

    def test_the_prompt_names_the_three_parts_of_a_citation(self):
        block = profile_schema.schema_prompt_block()
        for part in ("source", "locator", "quote"):
            self.assertIn(part, block)


class WhatComesBackIsCoerced(TestCase):

    def _normalize(self, sources):
        raw = {"sections": {"company_profile": {
            "data": {"website": "https://tyreplex.com"},
            "sources": sources}}}
        return profile_schema.normalize_profile(raw)["sections"][
            "company_profile"]["sources"]

    def test_a_good_citation_survives(self):
        out = self._normalize({"website": {
            "source": TEASER, "locator": "slide 3",
            "quote": "www.tyreplex.com"}})
        self.assertEqual(out["website"]["source"], TEASER)
        self.assertEqual(out["website"]["locator"], "slide 3")
        self.assertEqual(out["website"]["quote"], "www.tyreplex.com")

    def test_a_citation_naming_no_source_is_dropped(self):
        """It names nothing, so it is evidence of nothing."""
        out = self._normalize({"website": {"quote": "www.tyreplex.com"}})
        self.assertEqual(out, {})

    def test_a_malformed_block_is_dropped_not_raised(self):
        self.assertEqual(self._normalize("not a mapping"), {})
        self.assertEqual(self._normalize({"website": "a bare string"}), {})

    def test_a_missing_block_is_simply_absent(self):
        raw = {"sections": {"company_profile": {"data": {"website": "x"}}}}
        section = profile_schema.normalize_profile(raw)["sections"][
            "company_profile"]
        self.assertEqual(section["sources"], {})

    def test_the_dropped_block_is_reported_to_the_run(self):
        notes = []
        profile_schema.normalize_profile(
            {"sections": {"company_profile": {
                "data": {"website": "x"},
                "sources": {"website": {"locator": "slide 3"}}}}},
            notes=notes)
        self.assertTrue(any("could not be read as citations" in n
                            for n in notes))


class TheEndpointServesTheCitation(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.models import ProfileSection
        from fundos.profile.section_writer import update_section_from_data
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        self.profile = get_or_create_profile(
            self.world["a"]["company"], user=self.world["a"]["founder"])
        update_section_from_data(
            self.profile, "company_profile",
            {"description_of_business": "A B2B marketplace for tyres.",
             "website": "https://tyreplex.com"},
            user=self.world["a"]["founder"])

        row = ProfileSection.objects.filter(
            profile=self.profile,
            section_key=profile_schema.storage_key_for("company_profile"),
            is_active=True).first()
        row.field_sources = {"website": {
            "source": TEASER, "locator": "slide 3",
            "quote": "www.tyreplex.com"}}
        row.save(update_fields=["field_sources"])

    def _get(self, field_id):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/{field_id}/sources",
            **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_the_double_underscore_id_is_parsed(self):
        """The split was on a dot, so this was empty on every real call."""
        body = self._get("company_profile__website")
        self.assertEqual(body["sectionKey"], "company_profile")
        self.assertEqual(body["field"], "website")

    def test_a_cited_field_returns_the_document_slide_and_quote(self):
        body = self._get("company_profile__website")
        self.assertTrue(body["cited"])
        source = body["sources"][0]
        self.assertEqual(source["title"], f"{TEASER} · slide 3")
        self.assertEqual(source["type"], "document")
        self.assertEqual(source["snippet"], "www.tyreplex.com")

    def test_an_uncited_field_says_so_rather_than_implying_evidence(self):
        """The caller has to be able to tell a citation from a generality."""
        body = self._get("company_profile__description_of_business")
        self.assertFalse(body["cited"])

    def test_a_web_sourced_value_is_typed_as_web(self):
        from fundos.profile.models import ProfileSection

        row = ProfileSection.objects.filter(
            profile=self.profile,
            section_key=profile_schema.storage_key_for("company_profile"),
            is_active=True).first()
        row.field_sources = {"website": {
            "source": "Web Research — Company Basics & Identity",
            "locator": "", "quote": "Tyreplex operates tyreplex.com."}}
        row.save(update_fields=["field_sources"])

        source = self._get("company_profile__website")["sources"][0]
        self.assertEqual(source["type"], "web")
        # No locator, so no dangling separator in the title.
        self.assertNotIn("·", source["title"])

    def test_an_unknown_field_is_not_an_error(self):
        body = self._get("company_profile__no_such_field")
        self.assertFalse(body["cited"])

    def test_a_citation_never_carries_a_url(self):
        """Same rule the assessment citations follow."""
        for source in self._get("company_profile__website")["sources"]:
            self.assertFalse(source.get("url"))


class AListItemIsCitedByItsOwnId(TestCase):
    """The model cites an array item by POSITION, because position is all it
    can see. Position is the one thing that cannot be stored: insert a founder
    at the top on the next run and every citation below it describes somebody
    else. It is resolved to the item's id at write time."""

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.profile = get_or_create_profile(
            self.world["a"]["company"], user=self.world["a"]["founder"])

    def _write(self, sources):
        from fundos.profile.models import ProfileSection
        from fundos.profile.pipeline import writer

        writer.write_profile(self.profile, {"sections": {
            "products_services": {
                "data": [{"name": "Online Tyre Retail", "category": "B2C",
                          "description": "Tyres online."},
                         {"name": "Motozee Stores", "category": "Retail",
                          "description": "Owned stores."}],
                "sources": sources}}},
            user=self.world["a"]["founder"])
        return ProfileSection.objects.filter(
            profile=self.profile, section_key="products_services",
            is_active=True).first()

    def test_a_position_is_stored_as_the_items_id(self):
        row = self._write({"1": {"source": TEASER, "locator": "slide 9",
                                 "quote": "Motozee retail network."}})
        items = row.structured
        if isinstance(items, dict):
            items = items.get("items") or []
        second_id = items[1]["id"]

        self.assertIn(second_id, row.field_sources)
        self.assertNotIn("1", row.field_sources)
        self.assertEqual(row.field_sources[second_id]["locator"], "slide 9")

    def test_a_position_that_names_no_item_is_dropped(self):
        row = self._write({"7": {"source": TEASER, "quote": "nothing here"}})
        self.assertEqual(row.field_sources, {})

    def test_a_non_numeric_key_on_a_list_is_dropped(self):
        row = self._write({"name": {"source": TEASER, "quote": "x"}})
        self.assertEqual(row.field_sources, {})

    def test_the_same_key_addresses_the_citation_and_the_confirmation(self):
        """One address for both, so a panel can fetch one from the other."""
        row = self._write({"0": {"source": TEASER, "quote": "Tyres online."}})
        items = row.structured
        if isinstance(items, dict):
            items = items.get("items") or []
        first_id = items[0]["id"]

        row.confirmed_fields = [first_id]
        row.save(update_fields=["confirmed_fields"])

        self.assertIn(first_id, row.field_sources)
        self.assertIn(first_id, row.confirmed_fields)


class TheWholeChain(TestCase):
    """Model response -> normalize -> write -> GET, with nothing planted.

    Every other test in this file exercises one link. This is the only one
    that proves they join: that the shape `normalize_profile` emits is the
    shape `write_profile` consumes, and that what the endpoint serves is what
    the model actually said. Three passing unit tests either side of a broken
    seam still add up to a feature that does not work, which is exactly how
    the dot-versus-double-underscore bug survived in this endpoint as long as
    it did.
    """

    #: What the synthesis call comes back with, in its own shape: a section
    #: wrapper carrying `data` and `sources`, an array section cited by
    #: position, a citation naming no source, and one field left uncited.
    RESPONSE = {
        "sections": {
            "company_profile": {
                "data": {
                    "description_of_business": "A B2B marketplace for tyres.",
                    "website": "https://tyreplex.com",
                    "country": "IN",
                },
                "sources": {
                    "description_of_business": {
                        "source": TEASER, "locator": "slide 2",
                        "quote": "TyrePlex is a B2B e-commerce platform."},
                    "website": {
                        "source": "Web Research - Company Basics & Identity",
                        "locator": "",
                        "quote": "tyreplex.com is the storefront."},
                    # Names nothing, so it must not survive.
                    "country": {"locator": "slide 2"},
                },
            },
            "products_services": {
                "data": [
                    {"name": "Online Tyre Retail", "category": "B2C",
                     "description": "Tyres for cars and commercial vehicles."},
                    {"name": "Motozee Stores", "category": "Retail",
                     "description": "Company-owned retail network."},
                ],
                "sources": {
                    "1": {"source": TEASER, "locator": "slide 9",
                          "quote": "Motozee stores bring structure."},
                },
            },
        }
    }

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.pipeline import writer
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])

        # The real boundary, then the real writer. No hand-planted rows.
        normalized = profile_schema.normalize_profile(self.RESPONSE)
        writer.write_profile(profile, normalized,
                             user=self.world["a"]["founder"])

    def _sources(self, field_id):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/{field_id}/sources",
            **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def _profile(self):
        return self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers).json()

    def _item(self, name):
        items = self._profile()["sections"]["products_services"]["data"]
        return next(i for i in items if i["name"] == name)

    # -- object section --------------------------------------------------
    def test_a_document_citation_survives_the_whole_chain(self):
        body = self._sources("company_profile__description_of_business")
        self.assertTrue(body["cited"])
        source = body["sources"][0]
        self.assertEqual(source["title"], TEASER + " · slide 2")
        self.assertEqual(source["type"], "document")
        self.assertEqual(source["snippet"],
                         "TyrePlex is a B2B e-commerce platform.")

    def test_a_web_citation_survives_and_is_typed_web(self):
        source = self._sources("company_profile__website")["sources"][0]
        self.assertEqual(source["type"], "web")
        self.assertIn("Company Basics", source["title"])

    def test_the_citation_naming_no_source_never_arrives(self):
        self.assertFalse(self._sources("company_profile__country")["cited"])

    # -- array section ---------------------------------------------------
    def test_an_item_is_cited_by_the_id_the_api_returns(self):
        """The id a client reads from the profile is the id it queries
        sources with: no translation, no position arithmetic."""
        second = self._item("Motozee Stores")
        body = self._sources("products_services__" + second["id"])
        self.assertTrue(body["cited"], "the item id did not resolve")
        self.assertEqual(body["sources"][0]["title"],
                         TEASER + " · slide 9")
        self.assertIn("Motozee stores bring structure",
                      body["sources"][0]["snippet"])

    def test_an_uncited_item_is_not_given_neighbouring_evidence(self):
        first = self._item("Online Tyre Retail")
        body = self._sources("products_services__" + first["id"])
        self.assertFalse(body["cited"])

    def test_the_citation_did_not_follow_the_position(self):
        """Position 1 must address nothing: what was stored is an id."""
        self.assertFalse(self._sources("products_services__1")["cited"])

    # -- the join with confirmation --------------------------------------
    def test_one_id_addresses_both_evidence_and_confirmation(self):
        second = self._item("Motozee Stores")
        patch = self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/products_services",
            {"confirmed_fields": [second["id"]]},
            content_type="application/json", **self.headers)
        self.assertEqual(patch.status_code, status.HTTP_200_OK)

        entry = next(b for b in patch.json()["readinessBreakdown"]
                     if b["sectionKey"] == "products_services")
        self.assertEqual(entry["confirmed"], 1)
        # And the same id still fetches the evidence it was confirmed on.
        self.assertTrue(
            self._sources("products_services__" + second["id"])["cited"])

    def test_confirming_does_not_disturb_the_citations(self):
        second = self._item("Motozee Stores")
        before = self._sources("products_services__" + second["id"])
        self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/products_services",
            {"confirmed_fields": [second["id"]]},
            content_type="application/json", **self.headers)
        self.assertEqual(
            self._sources("products_services__" + second["id"]), before)


class AnEntityBackedSectionIsCitedToo(TestCase):
    """Founders, competitors, funding rounds and news live in their OWN
    TABLES and leave `structured` empty.

    Resolution used to read `structured` directly, which got object sections
    and JSON lists right and these badly wrong: competitors stored raw
    positions like "0", which address nothing, and founders stored nothing at
    all. Every one of those sections looked populated in the database and
    returned `cited: false` from the endpoint.
    """

    RESPONSE = {
        "sections": {
            "founders": {
                "data": [
                    {"name": "Puneet Bhaskar", "role": "Founder & CEO",
                     "background": "Fifteen years in the tyre aftermarket.",
                     "is_founder": True},
                    {"name": "Anand Kumar", "role": "CTO",
                     "background": "Built the dealer platform.",
                     "is_founder": True},
                ],
                "sources": {
                    "1": {"source": TEASER, "locator": "slide 20",
                          "quote": "Anand Kumar, CTO, built the platform."},
                },
            },
            "competitors": {
                "data": [
                    {"name": "Tyremarket", "description": "Online retailer."},
                    {"name": "Apollo Direct", "description": "OEM channel."},
                ],
                "sources": {
                    "0": {"source": "Web Research - Competitive Landscape",
                          "locator": "",
                          "quote": "Tyremarket sells tyres online."},
                },
            },
        }
    }

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.pipeline import writer
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        writer.write_profile(
            profile, profile_schema.normalize_profile(self.RESPONSE),
            user=self.world["a"]["founder"])

    def _profile(self):
        return self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers).json()

    def _sources(self, field_id):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/{field_id}/sources",
            **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def _item(self, section, name):
        items = self._profile()["sections"][section]["data"]
        return next(i for i in items if i["name"] == name)

    def test_a_founder_is_cited_by_their_row_id(self):
        cto = self._item("founders", "Anand Kumar")
        body = self._sources("founders__" + cto["id"])
        self.assertTrue(body["cited"], "the founder row id did not resolve")
        self.assertEqual(body["sources"][0]["title"], TEASER + " · slide 20")
        self.assertIn("Anand Kumar, CTO", body["sources"][0]["snippet"])

    def test_the_uncited_founder_gets_nobody_elses_evidence(self):
        ceo = self._item("founders", "Puneet Bhaskar")
        self.assertFalse(self._sources("founders__" + ceo["id"])["cited"])

    def test_a_competitor_is_cited_by_its_row_id(self):
        rival = self._item("competitors", "Tyremarket")
        body = self._sources("competitors__" + rival["id"])
        self.assertTrue(body["cited"])
        self.assertEqual(body["sources"][0]["type"], "web")

    def test_no_raw_position_is_ever_stored(self):
        """"0" and "1" address nothing and must never reach the column."""
        from fundos.profile.models import ProfileSection
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        for key in ("founders", "competitors"):
            row = ProfileSection.objects.filter(
                profile=profile,
                section_key=profile_schema.storage_key_for(key),
                is_active=True).first()
            for stored_key in (row.field_sources or {}):
                self.assertFalse(stored_key.isdigit(),
                                 f"{key} stored the position {stored_key!r}")

    def test_the_citation_follows_the_name_not_the_order(self):
        """Rows come back from the database in no guaranteed order, so a
        citation matched by index would attach to whichever founder happened
        to be returned second."""
        cto = self._item("founders", "Anand Kumar")
        body = self._sources("founders__" + cto["id"])
        self.assertIn("Anand Kumar", body["sources"][0]["snippet"])


DECK = "TyrePlex Investor Deck Detailed June26 OS.pdf"
MODEL_XLSX = "Tyreplex_Financial Model_June26.xlsx"

DOSSIER = """# Consolidated research dossier: Tyreplex

# Source 1: Web Research (search-grounded)

## Batch 1: Company Basics & Identity

### 1. What is the company overview of Tyreplex?
Tyreplex is a B2B tyre marketplace.

#### Grounding sources

## Batch 2: Founders & Leadership

### 1. Who are the founders of Tyreplex?
Puneet Bhaskar is named as the CEO.

# Source 2: Company Documents (native extraction + document model)

## Company Presentation

### TyrePlex Investor Deck Detailed June26 OS.pdf

#### Extracted content

## Page 1

Building the OS for Tyre Trade in India. Puneet Bhaskar, Co-Founder - CEO,
IIM Kozhikode.

## Financial Model

### Tyreplex_Financial Model_June26.xlsx

#### Extracted content

Sheet: P&L. Revenue FY26: INR 412 Cr.
"""


class TheCitableSourcesAreHarvestedFromTheDossier(TestCase):

    def test_documents_come_first(self):
        """The ordering IS the instruction: a reader trusts the company's own
        deck over a search result repeating the same fact."""
        from fundos.profile.pipeline.dossier import source_labels

        labels, documents = source_labels(DOSSIER)
        self.assertEqual(documents, 2)
        self.assertEqual(labels[:2], [DECK, MODEL_XLSX])

    def test_every_research_batch_is_citable(self):
        from fundos.profile.pipeline.dossier import source_labels

        labels, _ = source_labels(DOSSIER)
        self.assertIn("Batch 1: Company Basics & Identity", labels)
        self.assertIn("Batch 2: Founders & Leadership", labels)

    def test_a_research_question_is_not_a_source(self):
        """Questions are also `###`, but only inside Source 1."""
        from fundos.profile.pipeline.dossier import source_labels

        labels, _ = source_labels(DOSSIER)
        self.assertFalse(
            any(label.startswith("1. What is") for label in labels))

    def test_structural_headings_are_not_sources(self):
        from fundos.profile.pipeline.dossier import source_labels

        labels, _ = source_labels(DOSSIER)
        for structural in ("Extracted content", "Grounding sources"):
            self.assertNotIn(structural, labels)

    def test_a_slide_heading_inside_the_deck_is_not_a_source(self):
        """A converted deck contributes its own `###` headings to Source 2.
        Harvested as sources they would put labels in front of the model that
        name no file anyone can open."""
        from fundos.profile.pipeline.dossier import source_labels

        dossier = DOSSIER + """
### Why now: the tyre trade is going digital

Some slide body text.
"""
        labels, docs = source_labels(dossier)
        self.assertEqual(docs, 2)
        self.assertNotIn("Why now: the tyre trade is going digital", labels)

    def test_an_empty_dossier_offers_nothing_to_cite(self):
        from fundos.profile.pipeline.dossier import source_labels

        self.assertEqual(source_labels(""), ([], 0))


class ThePromptShowsTheModelTheList(TestCase):

    def test_the_exact_filenames_are_put_in_front_of_it(self):
        from fundos.profile.pipeline.dossier import source_labels

        labels, _ = source_labels(DOSSIER)
        block = profile_schema.schema_prompt_block(source_labels=labels)
        self.assertIn(DECK, block)
        self.assertIn("copy one exactly", block)

    def test_the_documents_are_marked_as_documents(self):
        """"Prefer an uploaded document" is not an instruction the model can
        follow against a flat list it cannot tell apart."""
        from fundos.profile.pipeline.dossier import source_labels

        labels, docs = source_labels(DOSSIER)
        block = profile_schema.schema_prompt_block(
            source_labels=labels, document_count=docs)
        self.assertIn("UPLOADED DOCUMENTS (prefer these):", block)
        self.assertLess(block.index(DECK),
                        block.index("WEB RESEARCH"),
                        "the deck must be listed above the research batches")

    def test_the_document_is_named_as_the_preferred_source(self):
        block = profile_schema.schema_prompt_block()
        self.assertIn("PREFER AN UPLOADED DOCUMENT", block)


class ACitationMustNameSomethingReal(TestCase):

    LABELS = [DECK, "Batch 2: Founders & Leadership"]

    def _sources(self, claimed):
        raw = {"sections": {"company_profile": {
            "data": {"website": "https://tyreplex.com"},
            "sources": {"website": {"source": claimed, "locator": "Page 1",
                                    "quote": "Building the OS."}}}}}
        return profile_schema.normalize_profile(
            raw, allowed_sources=self.LABELS)["sections"][
                "company_profile"]["sources"]

    def test_the_generic_channel_label_is_rejected(self):
        """The exact string that produced 64 useless citations."""
        self.assertEqual(self._sources("Web Research (search-grounded)"), {})

    def test_a_misspelled_label_is_rejected(self):
        """"search-groundd" is what writing from memory looks like."""
        self.assertEqual(self._sources("Web Research (search-groundd)"), {})

    def test_the_exact_filename_is_kept(self):
        out = self._sources(DECK)
        self.assertEqual(out["website"]["source"], DECK)

    def test_a_batch_named_loosely_resolves_to_the_real_heading(self):
        out = self._sources("Web Research, Batch 2: Founders & Leadership")
        self.assertEqual(out["website"]["source"],
                         "Batch 2: Founders & Leadership")

    def test_batch_one_does_not_swallow_batch_ten(self):
        labels = ["Batch 1: Company Basics", "Batch 10: Investment Thesis"]
        self.assertEqual(
            profile_schema.canonical_source("Batch 10: Investment Thesis",
                                            labels),
            "Batch 10: Investment Thesis")

    def test_the_rejection_is_reported_to_the_run(self):
        notes = []
        profile_schema.normalize_profile(
            {"sections": {"company_profile": {
                "data": {"website": "x"},
                "sources": {"website": {"source": "Something invented",
                                        "quote": "q"}}}}},
            notes=notes, allowed_sources=self.LABELS)
        self.assertTrue(any("not in the dossier" in n for n in notes), notes)

    def test_without_a_list_nothing_is_rejected(self):
        """A caller with no dossier to check against keeps what it was given
        rather than discarding every citation."""
        raw = {"sections": {"company_profile": {
            "data": {"website": "x"},
            "sources": {"website": {"source": "Anything", "quote": "q"}}}}}
        out = profile_schema.normalize_profile(raw)["sections"][
            "company_profile"]["sources"]
        self.assertEqual(out["website"]["source"], "Anything")


class TheDocumentCitationReachesTheReader(TestCase):
    """Model response -> normalize (validated) -> write -> GET."""

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.pipeline import writer
        from fundos.profile.pipeline.dossier import source_labels
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])

        labels, _ = source_labels(DOSSIER)
        response = {"sections": {
            "company_profile": {
                "data": {"description_of_business": "A B2B tyre marketplace.",
                         "website": "https://tyreplex.com"},
                "sources": {
                    # Cited to the deck: the strong source.
                    "description_of_business": {
                        "source": DECK, "locator": "Page 1",
                        "quote": "Building the OS for Tyre Trade in India."},
                    # Cited to a channel that is not a dossier heading.
                    "website": {"source": "Web Research (search-grounded)",
                                "locator": "", "quote": "tyreplex.com"}},
            },
            "founders": {
                "data": [{"name": "Puneet Bhaskar", "role": "Co-Founder - CEO",
                          "background": "IIM Kozhikode.", "is_founder": True}],
                "sources": {"0": {
                    "source": DECK, "locator": "Page 1",
                    "quote": "Puneet Bhaskar, Co-Founder - CEO, IIM "
                             "Kozhikode."}},
            },
            "company_metrics": {
                "data": [{"metric": "Revenue FY26", "value": "412",
                          "unit": "INR Cr"}],
                "sources": {"0": {
                    "source": MODEL_XLSX, "locator": "Sheet: P&L",
                    "quote": "Revenue FY26: INR 412 Cr."}},
            },
        }}
        normalized = profile_schema.normalize_profile(
            response, allowed_sources=labels)
        writer.write_profile(profile, normalized,
                             user=self.world["a"]["founder"])

    def _sources(self, field_id):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/{field_id}/sources",
            **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def _profile(self):
        return self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers).json()

    def test_a_field_cites_the_deck_by_name_and_page(self):
        body = self._sources("company_profile__description_of_business")
        self.assertTrue(body["cited"])
        source = body["sources"][0]
        self.assertEqual(source["title"], DECK + " · Page 1")
        self.assertEqual(source["type"], "document")
        self.assertEqual(source["snippet"],
                         "Building the OS for Tyre Trade in India.")

    def test_the_invented_channel_citation_never_arrives(self):
        self.assertFalse(self._sources("company_profile__website")["cited"])

    def test_a_founder_cites_the_deck(self):
        founder = self._profile()["sections"]["founders"]["data"][0]
        body = self._sources("founders__" + founder["id"])
        self.assertTrue(body["cited"], "the founder lost their citation")
        self.assertIn(DECK, body["sources"][0]["title"])
        self.assertIn("Puneet Bhaskar", body["sources"][0]["snippet"])

    def test_a_metric_cites_the_spreadsheet_and_its_sheet(self):
        """Which file, which sheet, and the figure as printed."""
        metric = self._profile()["sections"]["company_metrics"]["data"][0]
        body = self._sources("company_metrics__" + metric["id"])
        self.assertTrue(body["cited"])
        self.assertEqual(body["sources"][0]["title"],
                         MODEL_XLSX + " · Sheet: P&L")
        self.assertEqual(body["sources"][0]["snippet"],
                         "Revenue FY26: INR 412 Cr.")

    def test_no_citation_carries_a_link(self):
        """A citation names a document and a page, never a URL."""
        for field in ("company_profile__description_of_business",):
            for source in self._sources(field)["sources"]:
                self.assertFalse(source.get("url"))

    def test_the_cited_value_can_also_be_confirmed_by_the_same_id(self):
        founder = self._profile()["sections"]["founders"]["data"][0]
        patch = self.client.patch(
            f"/api/v1/companies/{self.co}/profile/sections/founders",
            {"confirmed_fields": [founder["id"]]},
            content_type="application/json", **self.headers)
        self.assertEqual(patch.status_code, status.HTTP_200_OK)
        entry = next(b for b in patch.json()["readinessBreakdown"]
                     if b["sectionKey"] == "founders")
        self.assertEqual(entry["confirmed"], 1)
        self.assertTrue(self._sources("founders__" + founder["id"])["cited"])


class TheRunReportsWhereItsCitationsLanded(TestCase):
    """A run whose citations are all web research, for a company that
    uploaded an investor deck, is a bad run even when every section is
    populated. Nothing else in the run summary says so."""

    def test_citations_are_counted_against_the_documents(self):
        from fundos.profile.pipeline.synthesize import _citation_summary

        profile = {"sections": {
            "company_profile": {"sources": {
                "description_of_business": {"source": DECK},
                "website": {"source": "Batch 1: Company Basics & Identity"}}},
            "company_metrics": {"sources": {
                "m1": {"source": MODEL_XLSX},
                "m2": {"source": ""}}},
        }}
        self.assertEqual(
            _citation_summary(profile, [DECK, MODEL_XLSX]),
            {"cited": 3, "to_documents": 2, "to_research": 1})

    def test_a_run_with_no_documents_counts_none_against_them(self):
        from fundos.profile.pipeline.synthesize import _citation_summary

        profile = {"sections": {"company_profile": {"sources": {
            "website": {"source": "Batch 1: Company Basics & Identity"}}}}}
        summary = _citation_summary(profile, [])
        self.assertEqual(summary["to_documents"], 0)
        self.assertEqual(summary["to_research"], 1)


class AListSectionIsCitedWhateverShapeTheModelUses(TestCase):
    """Tyreplex's five founders arrived cited and reached the reader with
    nothing.

        founders: a sources block came back that could not be read as
                  citations - it was dropped

    The parser accepted exactly one shape. Asked to cite a list of PEOPLE,
    the model reasonably cited each person field by field -- and the outer
    dict then has no `source` key, so every citation for every founder was
    discarded. Four sections came back uncited from one strict reader.
    """

    def _sources(self, block):
        raw = {"sections": {"founders": {
            "data": [{"name": "Puneet Bhaskar", "role": "CEO"},
                     {"name": "Jiveshwar Sharma", "role": "CTO"}],
            "sources": block}}}
        return profile_schema.normalize_profile(raw)["sections"][
            "founders"]["sources"]

    def test_the_documented_shape_still_works(self):
        out = self._sources({"0": {"source": DECK, "locator": "Page 4",
                                   "quote": "Puneet Bhaskar, CEO"}})
        self.assertEqual(out["0"]["source"], DECK)

    def test_a_citation_per_field_collapses_to_one_per_person(self):
        """What actually broke Tyreplex."""
        out = self._sources({
            "0": {"name": {"source": DECK, "locator": "Page 4",
                           "quote": "Puneet Bhaskar"},
                  "role": {"source": DECK, "locator": "Page 4",
                           "quote": "Co-Founder - CEO"}}})
        self.assertEqual(out["0"]["source"], DECK)
        self.assertTrue(out["0"]["quote"])

    def test_the_quoted_citation_wins_when_collapsing(self):
        """A reader can check a quote; a bare source names a haystack."""
        out = self._sources({
            "0": {"name": {"source": DECK, "quote": ""},
                  "background": {"source": DECK, "locator": "Page 9",
                                 "quote": "IIM Kozhikode"}}})
        self.assertEqual(out["0"]["quote"], "IIM Kozhikode")
        self.assertEqual(out["0"]["locator"], "Page 9")

    def test_a_list_of_citations_is_read_by_position(self):
        out = self._sources([
            {"source": DECK, "locator": "Page 4", "quote": "Puneet"},
            {"source": DECK, "locator": "Page 4", "quote": "Jiveshwar"}])
        self.assertEqual(out["0"]["quote"], "Puneet")
        self.assertEqual(out["1"]["quote"], "Jiveshwar")

    def test_a_block_of_pure_noise_is_still_rejected(self):
        """Widening what is accepted must not mean accepting anything."""
        self.assertEqual(self._sources({"0": {"locator": "Page 4"}}), {})

    def test_an_unreadable_shape_is_named_in_the_run(self):
        """The silent path is what hid this: three more sections came back
        uncited and the run said nothing at all about them."""
        notes = []
        profile_schema.normalize_profile(
            {"sections": {"founders": {"data": [], "sources": "Page 4"}}},
            notes=notes)
        self.assertTrue(any("not a citation map" in n for n in notes), notes)


class ACitationIsTypedFromTheSourceNotTheTitle(TestCase):
    """Every web citation in every profile came back `type: "document"`.

    The type was decided from the CLEANED title -- the one string that cannot
    answer it, because the cleaner strips the "Batch 3: " prefix and the test
    then asked whether the result started with "batch". A file icon and a
    filename affordance over a search topic, promising a page the reader
    could turn to.
    """

    def _kind(self, source):
        from fundos.profile.views import _source_kind
        return _source_kind(source)

    def test_a_research_batch_is_web(self):
        self.assertEqual(
            self._kind("Batch 3: Products, Services & Business Model"), "web")

    def test_a_topic_that_lost_its_prefix_is_still_web(self):
        """The exact string that was being served as a document."""
        self.assertEqual(
            self._kind("Products, Services & Business Model"), "web")

    def test_a_pdf_is_a_document(self):
        self.assertEqual(self._kind(DECK), "document")

    def test_a_spreadsheet_is_a_document(self):
        self.assertEqual(self._kind(MODEL_XLSX), "document")

    def test_a_filename_with_dots_in_it_is_a_document(self):
        self.assertEqual(
            self._kind("Tyreplex_Financial Model_June26_ v2.1 final.xlsx"),
            "document")

    def test_a_topic_ending_in_a_word_is_not_mistaken_for_a_file(self):
        self.assertEqual(self._kind("Funding History & Investors"), "web")


class EveryEntityBackedSectionReachesTheReader(TestCase):
    """Founders, competitors, funding rounds and news live in their OWN
    tables. All four came back uncited, and only founders produced a
    complaint -- the other three were dropped down a path that says nothing,
    so "the model sent none" and "the parser refused the shape" looked
    identical from the outside.
    """

    RESPONSE = {"sections": {
        "founders": {
            "data": [{"name": "Puneet Bhaskar", "role": "Co-Founder & CEO",
                      "background": "Droom, Snapdeal.", "is_founder": True}],
            # Per-field, the shape that broke Tyreplex.
            "sources": {"0": {
                "name": {"source": DECK, "locator": "Page 4",
                         "quote": "Puneet Bhaskar, Co-Founder - CEO"}}},
        },
        "competitors": {
            "data": [{"name": "Tyremarket", "description": "Online retail."}],
            "sources": {"0": {"source": "Batch 8: Competitive Landscape",
                              "locator": "Who competes with Tyreplex?",
                              "quote": "Tyremarket sells tyres online."}},
        },
        "funding_history": {
            "data": [{"round": "Series A", "amount": "5",
                      "currency": "USD", "investors": "Blume"}],
            # Keyed by the round's NAME rather than its position.
            "sources": {"Series A": {
                "source": "Batch 6: Funding History & Investors",
                "locator": "What funding has Tyreplex raised?",
                "quote": "Tyreplex raised a USD 5M Series A."}},
        },
        "news": {
            "data": [{"title": "Tyreplex raises Series A", "date": "2026-03-01",
                      "summary": "Coverage of the round."}],
            "sources": {"0": {"source": "Batch 7: Recent News",
                              "locator": "", "quote": "Tyreplex raises."}},
        },
    }}

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.pipeline import writer
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        writer.write_profile(
            profile, profile_schema.normalize_profile(self.RESPONSE),
            user=self.world["a"]["founder"])

    def _profile(self):
        return self.client.get(
            f"/api/v1/companies/{self.co}/profile", **self.headers).json()

    def _sources(self, section, index=0):
        item = self._profile()["sections"][section]["data"][index]
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"{section}__{item['id']}/sources", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_a_founder_cited_field_by_field_is_cited(self):
        body = self._sources("founders")
        self.assertTrue(body["cited"], "the founders regression is back")
        self.assertEqual(body["sources"][0]["type"], "document")
        self.assertIn("Page 4", body["sources"][0]["title"])

    def test_a_competitor_is_cited(self):
        body = self._sources("competitors")
        self.assertTrue(body["cited"])
        self.assertEqual(body["sources"][0]["type"], "web")

    def test_a_funding_round_cited_by_name_is_cited(self):
        """A model that has just written a list of rounds will as readily key
        its citations by round name as by index."""
        body = self._sources("funding_history")
        self.assertTrue(body["cited"], "a name-keyed citation was dropped")
        self.assertIn("Series A", body["sources"][0]["snippet"])

    def test_a_news_item_is_cited(self):
        body = self._sources("news")
        self.assertTrue(body["cited"])

    def test_every_web_citation_says_web_research(self):
        for section in ("competitors", "funding_history", "news"):
            title = self._sources(section)["sources"][0]["title"]
            self.assertIn("Web Research", title, section)

    def test_no_entity_section_stores_a_raw_position(self):
        from fundos.profile.models import ProfileSection
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        for key in ("founders", "competitors", "funding_history", "news"):
            row = ProfileSection.objects.filter(
                profile=profile,
                section_key=profile_schema.storage_key_for(key),
                is_active=True).first()
            self.assertTrue(row and row.field_sources, f"{key} stored nothing")
            for stored in row.field_sources:
                self.assertFalse(stored.isdigit(),
                                 f"{key} stored the position {stored!r}")


class ACitationIsServedInTwoHalves(TestCase):
    """`name` and `locator` separately, so the frontend lays them out.

    A web citation's locator is the research question it answered. Glued onto
    the source it produced a 150-character title that wrapped over three lines
    and buried the source it was supposed to name.
    """

    def test_a_page_reference_is_short_enough_to_keep_whole(self):
        from fundos.profile.views import _short_locator

        for locator in ("Page 12", "Slide 4", "Consolidated P&L, Row 22"):
            self.assertEqual(_short_locator(locator), locator)

    def test_a_research_question_is_cut_for_the_title(self):
        from fundos.profile.views import _short_locator

        question = ("What are the products and services offered by Tyreplex "
                    "(https://www.tyreplex.com/)? Describe each offering.")
        short = _short_locator(question)
        self.assertLess(len(short), 60)
        self.assertTrue(short.endswith("…"))

    def test_no_locator_yields_nothing_rather_than_an_ellipsis(self):
        from fundos.profile.views import _short_locator

        self.assertEqual(_short_locator(""), "")
        self.assertEqual(_short_locator(None), "")


class TheTwoHalvesReachTheEndpoint(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.pipeline import writer
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        writer.write_profile(profile, profile_schema.normalize_profile(
            {"sections": {"company_profile": {
                "data": {"description_of_business": "A B2B tyre marketplace.",
                         "website": "https://tyreplex.com"},
                "sources": {
                    "description_of_business": {
                        "source": DECK, "locator": "Page 12",
                        "quote": "Motozee — solving for the customers."},
                    "website": {
                        "source": "Batch 1: Company Basics & Identity",
                        "locator": "Where is Tyreplex headquartered, and what "
                                   "is its registered corporate entity name?",
                        "quote": "tyreplex.com"}}}}}),
            user=self.world["a"]["founder"])

    def _source(self, field):
        body = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/{field}/sources",
            **self.headers).json()
        self.assertTrue(body["cited"], field)
        return body["sources"][0]

    def test_a_document_names_the_file_and_the_page(self):
        source = self._source("company_profile__description_of_business")
        self.assertEqual(source["name"], DECK)
        self.assertEqual(source["locator"], "Page 12")
        self.assertEqual(source["type"], "document")
        self.assertEqual(source["title"], f"{DECK} · Page 12")

    def test_web_research_says_so_and_keeps_the_full_question(self):
        source = self._source("company_profile__website")
        self.assertTrue(source["name"].startswith("Web Research"))
        self.assertEqual(source["type"], "web")
        self.assertIn("registered corporate entity name",
                      source["locator"],
                      "the full question must survive in `locator`")

    def test_the_title_stays_readable_on_one_line(self):
        source = self._source("company_profile__website")
        self.assertLess(len(source["title"]), 110)

    def test_the_quote_is_served_verbatim(self):
        source = self._source("company_profile__description_of_business")
        self.assertEqual(source["snippet"],
                         "Motozee — solving for the customers.")


class ACitationInsideTheStructureStillFindsItsField(TestCase):
    """The model cites INTO the data it just wrote.

    Asked for the provenance of `financial_summary.financials` it does not
    answer once about the table; it answers about `financials.0.pat_m` and
    `observations.3` and `investors_list.4.date`. That is a better answer to
    a question the UI never asks, because the UI addresses whole fields.

    Tyreplex stored 54 citations for `financial_summary` and 61 for the cap
    table and served `cited: false` for both -- real, paid-for, verbatim
    evidence overlapping the requested address exactly nowhere.
    """

    STORE = {
        "usp": {"source": "Batch 10: Company Story",
                "locator": "What is the USP?", "quote": "Widest network."},
        "financials.0.pat_m": {"source": XLSX, "locator": "P&L, Row 27",
                               "quote": "FY23: -5.0"},
        "financials.1.pat_m": {"source": XLSX, "locator": "P&L, Row 27",
                               "quote": "FY24: -3.1"},
        "financials.10.revenue_m": {"source": XLSX, "locator": "P&L, Row 5",
                                    "quote": "FY26: 412"},
        "financials.2.pat_m": {"source": XLSX, "locator": "P&L, Row 27",
                               "quote": "FY23: -5.0"},
        "observations.0": {"source": "Batch 7: Financial Performance",
                           "locator": "", "quote": "EBITDA negative."},
    }

    def _found(self, address, store=None):
        from fundos.profile.views import _citations_for
        return _citations_for(store if store is not None else self.STORE,
                              address)

    def test_a_plain_field_name_still_matches_exactly(self):
        found = self._found("usp")
        self.assertEqual(len(found), 1)
        self.assertEqual(found[0]["quote"], "Widest network.")

    def test_citations_inside_a_list_are_collected_under_its_field(self):
        self.assertEqual(len(self._found("observations")), 1)
        self.assertGreaterEqual(len(self._found("financials")), 2)

    def test_the_same_row_cited_twice_is_shown_once(self):
        """Twelve cells out of one P&L row cite that row twelve times."""
        quotes = [c["quote"] for c in self._found("financials")]
        self.assertEqual(len(quotes), len(set(quotes)))

    def test_rows_come_back_in_the_order_the_data_reads(self):
        """`financials.10` must not sort before `financials.2`."""
        quotes = [c["quote"] for c in self._found("financials")]
        self.assertEqual(quotes, ["FY23: -5.0", "FY24: -3.1", "FY26: 412"])

    def test_a_field_with_nothing_recorded_finds_nothing(self):
        self.assertEqual(self._found("cap_table_summary"), [])

    def test_a_prefix_that_is_not_a_path_segment_does_not_match(self):
        """`financials` must not collect `financials_summary.0`."""
        store = {"financials_summary.0": {"source": XLSX, "quote": "no"}}
        self.assertEqual(self._found("financials", store), [])

    def test_the_list_is_capped(self):
        store = {f"rows.{i}": {"source": XLSX, "locator": f"Row {i}",
                               "quote": f"q{i}"} for i in range(40)}
        self.assertLessEqual(len(self._found("rows", store)), 12)

    def test_a_citation_naming_no_source_is_not_counted(self):
        store = {"rows.0": {"locator": "Row 1", "quote": "q"}}
        self.assertEqual(self._found("rows", store), [])


class TheNestedCitationsReachTheEndpoint(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.models import ProfileSection
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        row, _ = ProfileSection.objects.get_or_create(
            profile=profile,
            section_key=profile_schema.storage_key_for("financial_summary"),
            defaults={"tenant_id": profile.tenant_id, "is_active": True})
        row.is_active = True
        row.field_sources = {
            "financials.0.pat_m": {"source": MODEL_XLSX,
                                   "locator": "Consolidated P&L, Row 27",
                                   "quote": "PBT FY26 -5.204173"},
            "financials.1.revenue_m": {"source": MODEL_XLSX,
                                       "locator": "Consolidated P&L, Row 5",
                                       "quote": "Revenue FY26 412"},
        }
        row.save()

    def test_the_field_now_cites_the_rows_it_was_built_from(self):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"financial_summary__financials/sources", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        body = response.json()
        self.assertTrue(body["cited"], "54 stored citations served nothing")
        self.assertEqual(len(body["sources"]), 2)
        self.assertEqual({s["locator"] for s in body["sources"]},
                         {"Consolidated P&L, Row 27",
                          "Consolidated P&L, Row 5"})

    def test_each_one_names_the_spreadsheet_as_a_document(self):
        body = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"financial_summary__financials/sources", **self.headers).json()
        for source in body["sources"]:
            self.assertEqual(source["type"], "document")
            self.assertEqual(source["name"], MODEL_XLSX)

    def test_the_ids_are_distinct(self):
        body = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"financial_summary__financials/sources", **self.headers).json()
        ids = [s["id"] for s in body["sources"]]
        self.assertEqual(len(ids), len(set(ids)))


class TheResponseNamesWhatItIsAbout(TestCase):
    """The id addresses the field; nothing named it.

        "field": "445a49fc-e94c-431e-bda5-fd09f54a9431"

    An array item's address is a UUID, so the response said nothing a reader
    could recognise and the UI had to already know what it had asked about.
    """

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.section_writer import update_section_from_data
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        self.profile = get_or_create_profile(self.world["a"]["company"],
                                             user=self.world["a"]["founder"])
        update_section_from_data(
            self.profile, "founders",
            [{"name": "Puneet Bhaskar", "role": "Co-Founder & CEO",
              "background": "Droom, Snapdeal.", "is_founder": True}],
            user=self.world["a"]["founder"])
        update_section_from_data(
            self.profile, "funding_history",
            [{"round": "Series A", "amount": "5", "currency": "USD"}],
            user=self.world["a"]["founder"])

    def _body(self, field):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/{field}/sources",
            **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def _first(self, section):
        items = self.client.get(
            f"/api/v1/companies/{self.co}/profile",
            **self.headers).json()["sections"][section]["data"]
        return items[0]["id"]

    def test_a_founder_is_named_by_the_person(self):
        item = self._first("founders")
        body = self._body("founders__" + item)
        self.assertEqual(body["fieldName"], "Puneet Bhaskar")
        self.assertEqual(body["field"], item, "the id must not change")

    def test_a_funding_round_is_named_by_the_round(self):
        item = self._first("funding_history")
        self.assertEqual(self._body("funding_history__" + item)["fieldName"],
                         "Series A")

    def test_the_section_is_named_without_its_spec_number(self):
        item = self._first("founders")
        body = self._body("founders__" + item)
        self.assertEqual(body["sectionName"], "Founders & Key People")
        self.assertNotIn("8.2", body["sectionName"])

    def test_an_object_field_is_humanised(self):
        body = self._body("company_profile__description_of_business")
        self.assertEqual(body["fieldName"], "Description of business")

    def test_an_item_that_no_longer_exists_falls_back_to_its_address(self):
        """Never blank: an unnamed row is still addressable."""
        body = self._body("founders__11111111-2222-3333-4444-555555555555")
        self.assertEqual(body["fieldName"],
                         "11111111-2222-3333-4444-555555555555")

    def test_the_label_is_not_called_name(self):
        """Every source below carries a `name` of its own -- the source's.
        Two `name` keys one level apart is a trap."""
        body = self._body("company_profile__website")
        self.assertNotIn("name", body)

    def test_the_label_matches_what_a_citation_is_matched_by(self):
        """The name shown and the identity the citation was attached to must
        be one thing, or they drift."""
        from fundos.profile.pipeline.writer import _identity
        from fundos.profile.spec_serializer import serialize_section

        item = (serialize_section(self.profile, "founders")["data"] or [])[0]
        body = self._body("founders__" + item["id"])
        self.assertEqual(body["fieldName"].casefold(), _identity(item))


class AFieldNameReadsAsSomeoneWouldSayIt(TestCase):

    def _say(self, field):
        from fundos.profile.views import _humanize
        return _humanize(field)

    def test_underscores_become_spaces(self):
        self.assertEqual(self._say("origin_story"), "Origin story")

    def test_an_acronym_is_shouted_not_capitalised(self):
        self.assertEqual(self._say("usp"), "USP")
        self.assertEqual(self._say("ebitda_margin"), "EBITDA margin")

    def test_units_and_currencies_are_shouted_too(self):
        self.assertEqual(self._say("total_funding_raised_usd_mn"),
                         "Total funding raised USD MN")

    def test_a_single_word_is_capitalised(self):
        self.assertEqual(self._say("website"), "Website")

    def test_nothing_in_gives_nothing_out(self):
        self.assertEqual(self._say(""), "")
        self.assertEqual(self._say(None), "")


class WhatTheCompanyItselfSuppliedIsCitable(TestCase):
    """Two of Tyreplex's five founders came off the onboarding form.

    The founders block sits in the dossier but was never a citable label, so
    a founder whose name the company typed had NO valid source string. The
    model's three options were all bad -- cite a research batch it did not
    read the name from, cite the deck, or omit the citation -- and those two
    founders were permanently unattributable however many times you re-ran.
    """

    DOSSIER = """# Consolidated research dossier: Tyreplex

## Founders named by the company (user-provided, unverified — RESEARCH LEADS)

- Puneet Bhaskar — https://linkedin.com/in/puneet

# Source 1: Web Research (search-grounded)

## Batch 2: Founders & Leadership

# Source 2: Company Documents

### Deck.pdf
"""

    def _labels(self, dossier=None):
        from fundos.profile.pipeline.dossier import source_labels
        return source_labels(self.DOSSIER if dossier is None else dossier)

    def test_the_company_becomes_a_source_it_can_name(self):
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

        labels, _ = self._labels()
        self.assertIn(FOUNDER_SOURCE_LABEL, labels)

    def test_a_dossier_with_no_founders_block_offers_no_such_source(self):
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

        labels, _ = self._labels(
            self.DOSSIER.replace("## Founders named by the company",
                                 "## Something else"))
        self.assertNotIn(FOUNDER_SOURCE_LABEL, labels)

    def test_the_document_slice_is_still_exactly_the_documents(self):
        """Callers slice `labels[:document_count]`; adding a third kind must
        not shift a research batch into that slice."""
        labels, documents = self._labels()
        self.assertEqual(labels[:documents], ["Deck.pdf"])

    def test_the_prompt_puts_the_company_first(self):
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

        labels, documents = self._labels()
        block = profile_schema.sources_block(labels, documents)
        self.assertIn("THE COMPANY ITSELF", block)
        self.assertLess(block.index(FOUNDER_SOURCE_LABEL),
                        block.index("UPLOADED DOCUMENTS"))

    def test_the_company_label_is_listed_once_not_twice(self):
        """It must not also appear under web research."""
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

        labels, documents = self._labels()
        block = profile_schema.sources_block(labels, documents)
        self.assertEqual(block.count(FOUNDER_SOURCE_LABEL), 1)

    def test_the_rule_tells_the_model_when_to_use_it(self):
        block = profile_schema.schema_prompt_block()
        self.assertIn("COMPANY ITSELF", block)

    def test_a_citation_naming_the_company_survives_validation(self):
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

        labels, _ = self._labels()
        out = profile_schema.normalize_profile(
            {"sections": {"founders": {
                "data": [{"name": "Puneet Bhaskar"}],
                "sources": {"0": {"source": FOUNDER_SOURCE_LABEL,
                                  "locator": "",
                                  "quote": "Puneet Bhaskar"}}}}},
            allowed_sources=labels)["sections"]["founders"]["sources"]
        self.assertEqual(out["0"]["source"], FOUNDER_SOURCE_LABEL)

    def test_it_is_typed_as_neither_a_document_nor_web_research(self):
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL
        from fundos.profile.views import _source_kind

        self.assertEqual(_source_kind(FOUNDER_SOURCE_LABEL), "founder")


class TheCompanySuppliedCitationReachesTheReader(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.pipeline import writer
        from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL
        from fundos.profile.services import get_or_create_profile

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.label = FOUNDER_SOURCE_LABEL
        self.world = make_world()
        self.co = str(self.world["a"]["company"].id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(self.world["a"]["company"],
                                        user=self.world["a"]["founder"])
        writer.write_profile(profile, profile_schema.normalize_profile(
            {"sections": {"founders": {
                "data": [{"name": "Puneet Bhaskar", "role": "CEO",
                          "is_founder": True},
                         {"name": "Sunish Kumar", "role": "Co-Founder",
                          "is_founder": True}],
                "sources": {
                    # Named by the company on its own form.
                    "0": {"source": FOUNDER_SOURCE_LABEL, "locator": "",
                          "quote": "Puneet Bhaskar"},
                    # Discovered in the deck.
                    "1": {"source": DECK, "locator": "Page 4",
                          "quote": "Sunish Kumar, Co-Founder"}}}}}),
            user=self.world["a"]["founder"])

    def _sources(self, name):
        items = self.client.get(
            f"/api/v1/companies/{self.co}/profile",
            **self.headers).json()["sections"]["founders"]["data"]
        item = next(i for i in items if i["name"] == name)
        return self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"founders__{item['id']}/sources", **self.headers).json()

    def test_the_founder_the_company_named_is_cited_to_the_company(self):
        body = self._sources("Puneet Bhaskar")
        self.assertTrue(body["cited"],
                        "a founder off the onboarding form is still uncited")
        self.assertEqual(body["sources"][0]["type"], "founder")
        self.assertEqual(body["sources"][0]["name"], self.label)

    def test_it_says_provided_by_user(self):
        self.assertEqual(self._sources("Puneet Bhaskar")["sources"][0]["name"],
                         "Provided by user")

    def test_a_founder_found_in_the_deck_still_cites_the_deck(self):
        body = self._sources("Sunish Kumar")
        self.assertEqual(body["sources"][0]["type"], "document")
        self.assertIn("Page 4", body["sources"][0]["title"])

    def test_the_company_citation_carries_no_locator_and_needs_none(self):
        source = self._sources("Puneet Bhaskar")["sources"][0]
        self.assertEqual(source["locator"], "")
        self.assertEqual(source["title"], self.label)
