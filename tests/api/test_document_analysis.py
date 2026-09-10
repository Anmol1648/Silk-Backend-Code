"""What we made of an uploaded document, served where a reader asks.

The Document Center reported `cited: false` for files the pipeline had read,
transcribed and critiqued. A document IS its own source, so "where did this
come from" has no interesting answer — but "what did you make of it" has
several, and every one of them was already generated and stored.

Two of them were not stored, in fact. The critique role's schema asks the
model for `inconsistencies` and `missing_information`, the model returns them,
and the task assigned only detected_type, summary, extracted_metrics and
recommendations. Both were generated, paid for, and dropped on every upload
ever made. What a reader saw in their place was derived from the FILENAME —
"filename may not match category" — a guess about the name of a file rendered
identically to a finding about its contents.
"""
from django.core.files.uploadedfile import SimpleUploadedFile
from django.core.management import call_command
from django.test import TestCase
from rest_framework import status

from tests.conftest_helpers import auth_headers, make_world


class AModelsFindingsSurviveWhateverShapeTheyArriveIn(TestCase):
    """Tolerant on the way in, strict on the way out."""

    def _clean(self, raw):
        from fundos.readiness.tasks import _string_list
        return _string_list(raw)

    def test_a_list_of_strings_passes_through(self):
        self.assertEqual(self._clean(["No team slide.", "No use of funds."]),
                         ["No team slide.", "No use of funds."])

    def test_a_list_of_objects_is_read_for_its_issue(self):
        """One dict in the list must not cost the other nine."""
        self.assertEqual(
            self._clean([{"issue": "Revenue stated two ways."},
                         "No team slide."]),
            ["Revenue stated two ways.", "No team slide."])

    def test_a_bare_string_is_treated_as_one_finding(self):
        self.assertEqual(self._clean("No team slide."), ["No team slide."])

    def test_an_unreadable_entry_is_dropped_not_rendered(self):
        """`{'issue': ...}` in front of a reader is worse than one fewer
        finding."""
        self.assertEqual(self._clean([{"unexpected": "x"}, 42, None,
                                      "Real finding."]),
                         ["Real finding."])

    def test_duplicates_are_collapsed(self):
        self.assertEqual(self._clean(["Same.", "Same."]), ["Same."])

    def test_nothing_in_gives_an_empty_list_not_a_crash(self):
        for empty in (None, [], "", {}, 0):
            self.assertEqual(self._clean(empty), [])

    def test_the_list_is_bounded(self):
        self.assertLessEqual(len(self._clean([f"finding {i}"
                                              for i in range(50)])), 20)


class TheDocumentCentreServesWhatWeKnow(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile
        from fundos.readiness.models import MaterialAsset

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        company = self.world["a"]["company"]
        self.co = str(company.id)
        self.headers = auth_headers(self.world["a"]["founder"])
        profile = get_or_create_profile(company,
                                        user=self.world["a"]["founder"])

        self.read = MaterialAsset.objects.create(
            tenant_id=company.tenant_id, deal_id=self.world["a"]["deal"].id,
            category="investment_material", detected_type="Product Brochure",
            summary="A brochure for the tyre marketplace.",
            extraction_status="extracted", ocr_applied=True,
            ocr_reason="Page content read by the document model.",
            inconsistencies=["Revenue stated as 412 Cr and 41.2 Cr."],
            missing_information=["Use of funds", "Team slide"],
            recommendations=[{"target_section": "Team",
                              "issue": "No team slide.",
                              "suggested_change": "Add one.",
                              "estimated_effort_min": 30}],
            extracted_metrics=[{"fieldKey": "revenue", "value": "412"}])
        self.doc = ProfileDocument.objects.create(
            profile=profile, tenant_id=company.tenant_id,
            category="company_presentation",
            filename="Brochure.pptx", material_asset_id=self.read.id)

        # A file that genuinely could not be opened.
        self.unread = MaterialAsset.objects.create(
            tenant_id=company.tenant_id, deal_id=self.world["a"]["deal"].id,
            category="other", extraction_status="failed",
            ocr_reason="The file is password protected.")
        self.locked = ProfileDocument.objects.create(
            profile=profile, tenant_id=company.tenant_id,
            category="other", filename="Locked.pdf",
            material_asset_id=self.unread.id)

    def _body(self, document):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"document_center__{document.id}/sources", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.json()

    def test_a_document_is_its_own_source(self):
        body = self._body(self.doc)
        self.assertTrue(body["cited"], "the Document Center cited nothing")
        self.assertEqual(body["sources"][0]["name"], "Brochure.pptx")
        self.assertEqual(body["sources"][0]["type"], "document")

    def test_the_detected_type_is_served(self):
        self.assertEqual(self._body(self.doc)["analysis"]["detectedType"],
                         "Product Brochure")

    def test_the_findings_the_model_made_are_served(self):
        analysis = self._body(self.doc)["analysis"]
        self.assertEqual(analysis["inconsistencies"],
                         ["Revenue stated as 412 Cr and 41.2 Cr."])
        self.assertEqual(analysis["missingInformation"],
                         ["Use of funds", "Team slide"])

    def test_the_recommendations_are_served(self):
        analysis = self._body(self.doc)["analysis"]
        self.assertEqual(len(analysis["recommendations"]), 1)
        self.assertEqual(analysis["recommendations"][0]["target_section"],
                         "Team")

    def test_the_document_is_named_for_the_reader(self):
        self.assertEqual(self._body(self.doc)["fieldName"], "Brochure.pptx")

    def test_a_read_file_says_it_was_read(self):
        extraction = self._body(self.doc)["analysis"]["extraction"]
        self.assertEqual(extraction["status"], "extracted")
        self.assertTrue(extraction["readByModel"])

    def test_an_unreadable_file_says_why_in_its_own_terms(self):
        """"We found nothing in this document" and "we could not open this
        document" must stop reading the same."""
        analysis = self._body(self.locked)["analysis"]
        self.assertEqual(analysis["extraction"]["status"], "failed")
        self.assertIn("password protected", analysis["extraction"]["reason"])

    def test_an_unreadable_file_claims_no_findings_it_does_not_have(self):
        analysis = self._body(self.locked)["analysis"]
        self.assertEqual(analysis["inconsistencies"], [])
        self.assertEqual(analysis["missingInformation"], [])

    def test_a_document_with_no_material_still_answers(self):
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile

        orphan = ProfileDocument.objects.create(
            profile=get_or_create_profile(self.world["a"]["company"],
                                          user=self.world["a"]["founder"]),
            tenant_id=self.world["a"]["company"].tenant_id,
            category="other", filename="Orphan.pdf")
        body = self._body(orphan)
        self.assertTrue(body["cited"])
        self.assertIsNone(body["analysis"])

    def test_an_id_that_names_no_document_is_not_an_error(self):
        response = self.client.get(
            f"/api/v1/companies/{self.co}/profile/fields/"
            f"document_center__11111111-2222-3333-4444-555555555555/sources",
            **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        self.assertFalse(response.json()["cited"])

    def test_another_companys_document_is_not_served(self):
        """The id is a UUID, but it is still someone else's."""
        other = str(self.world["b"]["company"].id)
        response = self.client.get(
            f"/api/v1/companies/{other}/profile/fields/"
            f"document_center__{self.doc.id}/sources",
            **auth_headers(self.world["b"]["founder"]))
        if response.status_code == status.HTTP_200_OK:
            self.assertFalse(response.json().get("cited"),
                             "a document leaked across companies")
            self.assertIn("cited", response.json(),
                          "`cited` belongs in every answer")


class TheStaleAnalysesCanBeRebuilt(TestCase):
    """An analysis is only as good as the text it was given, and for a long
    time every upload was given nothing. Those rows stay stale after the fix,
    because nothing re-reads a file that has already been processed."""

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.readiness.models import MaterialAsset

        self.world = make_world()
        company = self.world["a"]["company"]
        self.stale = MaterialAsset.objects.create(
            tenant_id=company.tenant_id, deal_id=self.world["a"]["deal"].id,
            category="company", extraction_status="failed",
            summary="Text could not be extracted from this file format.")
        self.good = MaterialAsset.objects.create(
            tenant_id=company.tenant_id, deal_id=self.world["a"]["deal"].id,
            category="company", extraction_status="extracted",
            summary="A real summary of real contents.")

    def test_the_dry_run_names_the_stale_rows_and_changes_nothing(self):
        from io import StringIO

        out = StringIO()
        call_command("reanalyse_materials", "--dry-run", stdout=out)
        printed = out.getvalue()
        self.assertIn(str(self.stale.id), printed)
        self.stale.refresh_from_db()
        self.assertEqual(self.stale.extraction_status, "failed")

    def test_a_healthy_row_is_left_alone(self):
        """Re-running it costs a model call to reproduce what is there."""
        from io import StringIO

        out = StringIO()
        call_command("reanalyse_materials", "--dry-run", stdout=out)
        self.assertNotIn(str(self.good.id), out.getvalue())

    def test_all_takes_everything(self):
        from io import StringIO

        out = StringIO()
        call_command("reanalyse_materials", "--dry-run", "--all", stdout=out)
        self.assertIn(str(self.good.id), out.getvalue())

    def test_it_can_be_scoped_to_one_company(self):
        from io import StringIO

        out = StringIO()
        call_command("reanalyse_materials", "--dry-run",
                     company=str(self.world["b"]["company"].id), stdout=out)
        self.assertNotIn(str(self.stale.id), out.getvalue())


class TheMaterialLinkIsWrittenAndRecoverable(TestCase):
    """`material_asset_id` has existed since the first migration and nothing
    ever wrote it.

    `register_material` returns the material; the create ignored it. So
    `_record_extraction` bailed on a null id and every Document Center row
    reported an empty handler, ocr_status and ocr_reason — the three fields
    that answer "was my file actually read?".
    """

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        from fundos.config.models import AppConfiguration

        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.company = self.world["a"]["company"]
        self.co = str(self.company.id)
        self.headers = auth_headers(self.world["a"]["founder"])

    def test_an_upload_records_which_material_it_became(self):
        from fundos.profile.models import ProfileDocument

        response = self.client.post(
            f"/api/v1/companies/{self.co}/documents",
            {"file": SimpleUploadedFile("deck.pdf", b"%PDF-1.4 tiny",
                                        content_type="application/pdf"),
             "category": "company_presentation"}, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_201_CREATED)
        row = ProfileDocument.objects.get(id=response.json()["id"])
        self.assertIsNotNone(row.material_asset_id,
                             "the upload lost its material link")

    def test_a_row_written_before_the_link_still_finds_its_material(self):
        """Both sides carry the same `document_id`, so every document already
        uploaded gets its analysis without a backfill."""
        import uuid

        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile
        from fundos.profile.views import _document_analysis
        from fundos.readiness.models import MaterialAsset

        profile = get_or_create_profile(self.company,
                                        user=self.world["a"]["founder"])
        shared = uuid.uuid4()
        MaterialAsset.objects.create(
            tenant_id=self.company.tenant_id,
            deal_id=self.world["a"]["deal"].id, category="company",
            document_id=shared, detected_type="Pitch Deck",
            extraction_status="extracted",
            missing_information=["Use of funds"])
        legacy = ProfileDocument.objects.create(
            profile=profile, tenant_id=self.company.tenant_id,
            category="company_presentation", filename="Legacy.pdf",
            document_id=shared, material_asset_id=None)

        _, analysis = _document_analysis(profile, str(legacy.id))
        self.assertIsNotNone(analysis, "an unlinked row found no analysis")
        self.assertEqual(analysis["detectedType"], "Pitch Deck")
        self.assertEqual(analysis["missingInformation"], ["Use of funds"])
