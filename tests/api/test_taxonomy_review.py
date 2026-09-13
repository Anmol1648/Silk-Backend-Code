"""A sector nobody curated is accepted; a benchmark cohort is never invented.

Thirty macro sectors and sixty sub-sectors were curated. A company whose
industry is not among them has to put something in the box, and blocking it
produces the worst outcome available: the founder picks the nearest value
that lets the form submit, and the profile then carries a label that is
wrong and looks deliberate.

So the label is accepted and marked. What must NOT happen is the second
half: a sub-sector is the benchmark join key, and creating a cohort for it
would score the company against a group holding no deals — Category F, a
fifth of the rating, reading as calculated when it rests on nothing. Blank
is the honest answer, and the roll-up already redistributes its weight.

The question a new label really raises is "which of our existing groups is
this?" — 'Digital Health' was not a missing sector, it was Healthtech under
another name. No string comparison settles that; a person settles it.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase
from rest_framework.test import APIClient

from fundos.assessment.models import SectorDealData, SectorMapping
from fundos.platformcfg import taxonomy
from fundos.platformcfg.lookup_models import Sector, SubSector
from tests.conftest_helpers import auth_headers


class TaxonomyCase(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_initial_data", verbosity=0)

    def setUp(self):
        Sector.objects.get_or_create(name="Healthcare")
        SubSector.objects.get_or_create(name="Healthtech")
        SectorDealData.objects.get_or_create(
            level="sub_sector", name="Healthtech",
            defaults={"clubbed_group": "Healthtech", "deal_count": 12,
                      "sample_quality": "OK", "source_file": "t.json"})


class ANewLabelIsAcceptedNotRefused(TaxonomyCase):

    def test_an_unknown_sub_sector_is_added(self):
        row, outcome = taxonomy.record_sub_sector("Digital Health",
                                                  company="Zyla")
        self.assertEqual(outcome, taxonomy.ADDED)
        self.assertTrue(row.is_pending)

    def test_it_records_which_company_introduced_it(self):
        row, _ = taxonomy.record_sub_sector("Digital Health", company="Zyla")
        self.assertEqual(row.added_from, "Zyla")

    def test_a_known_label_is_matched_not_duplicated(self):
        row, outcome = taxonomy.record_sub_sector("Healthtech")
        self.assertEqual(outcome, taxonomy.MATCHED)
        self.assertFalse(row.is_pending)
        self.assertEqual(SubSector.objects.filter(name="Healthtech").count(),
                         1)

    def test_case_and_spacing_do_not_create_a_second_row(self):
        """"healthtech" and "Healthtech" are one value, not two."""
        for spelling in ("healthtech", "  Healthtech  ", "HEALTHTECH"):
            _row, outcome = taxonomy.record_sub_sector(spelling)
            self.assertEqual(outcome, taxonomy.MATCHED, spelling)
        self.assertEqual(SubSector.objects.filter(
            name__iexact="Healthtech").count(), 1)

    def test_a_new_sub_sector_is_filed_under_its_sector(self):
        row, _ = taxonomy.record_sub_sector("Digital Health",
                                            sector="Healthcare")
        self.assertEqual(row.sector.name, "Healthcare")

    def test_a_new_sector_arrives_with_it_when_that_is_new_too(self):
        taxonomy.record("Space Economy", "Orbital Logistics", company="Acme")
        self.assertTrue(Sector.objects.get(name="Space Economy").is_pending)
        self.assertEqual(
            SubSector.objects.get(name="Orbital Logistics").sector.name,
            "Space Economy")

    def test_a_blank_is_not_a_value(self):
        for blank in ("", "   ", None):
            self.assertEqual(taxonomy.record_sub_sector(blank)[1],
                             taxonomy.IGNORED)
        self.assertFalse(SubSector.objects.filter(name="").exists())

    def test_recording_never_raises(self):
        """A taxonomy row is not worth failing a generation run over."""
        from unittest import mock

        with mock.patch("fundos.platformcfg.taxonomy.record_sector",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(taxonomy.record("X", "Y"),
                             {"sector": taxonomy.IGNORED,
                              "sub_sector": taxonomy.IGNORED})


class ACohortIsNeverInvented(TaxonomyCase):

    def test_adding_a_sub_sector_creates_no_benchmark_row(self):
        """The whole point. A cohort with no deals in it would score a
        company against nothing while looking calculated."""
        before = SectorDealData.objects.count()
        taxonomy.record_sub_sector("Digital Health", company="Zyla")
        self.assertEqual(SectorDealData.objects.count(), before)

    def test_adding_a_sub_sector_creates_no_alias(self):
        """Which group it belongs to is not something this can guess."""
        taxonomy.record_sub_sector("Digital Health")
        self.assertFalse(SectorMapping.objects.filter(
            raw_label__iexact="Digital Health").exists())

    def test_the_category_still_scores_blank_until_someone_maps_it(self):
        from fundos.profile.assessment_extraction import resolve_sub_sector

        taxonomy.record_sub_sector("Digital Health")
        self.assertEqual(resolve_sub_sector("Digital Health")[1],
                         "unresolved")


class APersonAnswersTheOneQuestion(TaxonomyCase):

    def test_the_queue_asks_it(self):
        taxonomy.record_sub_sector("Digital Health", company="Zyla")
        pending = taxonomy.pending()
        names = [row["name"] for row in pending["subSectors"]]
        self.assertIn("Digital Health", names)
        self.assertIn("Healthtech", pending["benchmarkGroups"])

    def test_the_queue_offers_the_groups_to_answer_with(self):
        self.assertIn("Healthtech", taxonomy.pending()["benchmarkGroups"])

    def test_answering_writes_the_alias_both_resolvers_read(self):
        from fundos.profile.assessment_extraction import resolve_sub_sector
        from fundos.profile.schema import canonical_sub_sector

        taxonomy.record_sub_sector("Digital Health")
        taxonomy.resolve_sub_sector("Digital Health", "Healthtech")

        self.assertEqual(resolve_sub_sector("Digital Health"),
                         ("Healthtech", "exact"))
        self.assertEqual(canonical_sub_sector("Digital Health"), "Healthtech")

    def test_answering_clears_the_pending_mark(self):
        taxonomy.record_sub_sector("Digital Health")
        taxonomy.resolve_sub_sector("Digital Health", "Healthtech")
        self.assertFalse(SubSector.objects.get(name="Digital Health")
                         .is_pending)
        self.assertEqual(taxonomy.pending()["subSectors"], [])

    def test_a_group_with_no_deal_data_is_refused(self):
        """Confirming it against an empty group hides the gap instead of
        closing it — the company is still scored against nothing."""
        taxonomy.record_sub_sector("Digital Health")
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.resolve_sub_sector("Digital Health", "Invented Group")

    def test_a_sub_sector_that_is_not_listed_is_refused(self):
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.resolve_sub_sector("Never Heard Of It", "Healthtech")

    def test_a_missing_answer_is_refused(self):
        taxonomy.record_sub_sector("Digital Health")
        with self.assertRaises(taxonomy.TaxonomyError):
            taxonomy.resolve_sub_sector("Digital Health", "")

    def test_a_sector_is_approved_without_implying_a_cohort(self):
        taxonomy.record_sector("Space Economy")
        taxonomy.approve_sector("Space Economy")
        self.assertFalse(Sector.objects.get(name="Space Economy").is_pending)
        self.assertFalse(SectorDealData.objects.filter(
            name="Space Economy").exists())


class ThroughTheApi(TaxonomyCase):

    def setUp(self):
        super().setUp()
        from fundos.core.models import Company, Membership, Tenant, User

        tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        self.user = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
            name="Admin User")
        company = Company.objects.create(
            tenant_id=tenant.id, name="Zyla", created_by=self.user)
        Membership.objects.create(
            tenant_id=tenant.id, user=self.user, scope_type="company",
            scope_id=company.id, role="founder", status="active")
        self.client = APIClient()
        self.headers = auth_headers(self.user)
        self.url = "/api/v1/config/taxonomy/review"

    def test_the_queue_is_served(self):
        taxonomy.record_sub_sector("Digital Health", company="Zyla")
        body = self.client.get(self.url, **self.headers).json()
        self.assertEqual(len(body["subSectors"]), 1)
        self.assertIn("Which benchmark group",
                      body["subSectors"][0]["question"])

    def test_answering_through_the_api(self):
        taxonomy.record_sub_sector("Digital Health", company="Zyla")
        response = self.client.post(
            self.url, {"subSector": "Digital Health", "group": "Healthtech"},
            format="json", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(SectorMapping.objects.filter(
            raw_label__iexact="Digital Health").exists())

    def test_an_empty_group_is_refused_with_a_reason(self):
        taxonomy.record_sub_sector("Digital Health", company="Zyla")
        response = self.client.post(
            self.url, {"subSector": "Digital Health", "group": "Invented"},
            format="json", **self.headers)
        self.assertEqual(response.status_code, 422)
        self.assertIn("deal data", response.json()["detail"])

    def test_a_nameless_review_is_refused(self):
        response = self.client.post(self.url, {"group": "Healthtech"},
                                    format="json", **self.headers)
        self.assertEqual(response.status_code, 400)


class ItHappensWhenAProfileIsSaved(TaxonomyCase):
    """The hook: whatever a company says its industry is becomes a picklist
    value, whether a founder typed it or synthesis wrote it."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        super().setUp()
        from fundos.core.models import Company, Membership, Tenant, User
        from fundos.profile.services import get_or_create_profile

        tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        self.user = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
            name="Saving User")
        self.company = Company.objects.create(
            tenant_id=tenant.id, name="Zyla Health", created_by=self.user)
        Membership.objects.create(
            tenant_id=tenant.id, user=self.user, scope_type="company",
            scope_id=self.company.id, role="founder", status="active")
        self.profile = get_or_create_profile(self.company, user=self.user)

    def _save(self, macro, sub):
        from fundos.profile.section_writer import update_section_from_data

        update_section_from_data(
            self.profile, "company_profile",
            {"description_of_business": "Care.", "website": "",
             "country": "IN", "macro_sector": macro, "sub_sector": sub,
             "funding_status_name": "", "revenue_size_name": "",
             "currency_id": "INR"},
            user=self.user)

    def test_saving_a_new_sub_sector_queues_it(self):
        self._save("Healthcare", "Chronic Care Management")
        row = SubSector.objects.get(name="Chronic Care Management")
        self.assertTrue(row.is_pending)
        self.assertEqual(row.added_from, "Zyla Health")

    def test_saving_a_known_one_changes_nothing(self):
        before = SubSector.objects.count()
        self._save("Healthcare", "Healthtech")
        self.assertEqual(SubSector.objects.count(), before)

    def test_the_save_still_succeeds(self):
        """A taxonomy row must never be what fails a profile write."""
        from fundos.profile.spec_serializer import serialize_section

        self._save("Healthcare", "Chronic Care Management")
        data = serialize_section(self.profile, "company_profile")["data"]
        self.assertEqual(data["sub_sector"], "Chronic Care Management")

class AFailingTaxonomyWriteCannotPoisonTheProfileWrite(TestCase):
    """`try/except` around a database call is not enough to make it harmless.

    This shipped ahead of its migration. The taxonomy insert hit a column
    that did not exist yet, the exception was caught as designed — and every
    later write in the same transaction then failed with "current
    transaction is aborted", losing the company_profile section. Postgres
    poisons a transaction on a failed statement whether or not anybody
    catches it; only a savepoint contains that.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        from fundos.core.models import Company, Membership, Tenant, User
        from fundos.profile.services import get_or_create_profile

        tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        self.user = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
            name="Saving User")
        self.company = Company.objects.create(
            tenant_id=tenant.id, name="Zyla Health", created_by=self.user)
        Membership.objects.create(
            tenant_id=tenant.id, user=self.user, scope_type="company",
            scope_id=self.company.id, role="founder", status="active")
        self.profile = get_or_create_profile(self.company, user=self.user)

    def _save_with_broken_taxonomy(self):
        """Write a section while the taxonomy tables are unusable."""
        from unittest import mock

        from django.db import ProgrammingError

        from fundos.profile.section_writer import update_section_from_data

        with mock.patch("fundos.platformcfg.taxonomy.record_sector",
                        side_effect=ProgrammingError(
                            "column lookup_sector.is_pending does not exist")):
            update_section_from_data(
                self.profile, "company_profile",
                {"description_of_business": "Care.", "website": "",
                 "country": "IN", "macro_sector": "Healthcare",
                 "sub_sector": "Healthtech", "funding_status_name": "",
                 "revenue_size_name": "", "currency_id": "INR"},
                user=self.user)

    def test_the_section_is_still_saved(self):
        from fundos.profile.spec_serializer import serialize_section

        self._save_with_broken_taxonomy()
        data = serialize_section(self.profile, "company_profile")["data"]
        self.assertEqual(data["description_of_business"], "Care.")
        self.assertEqual(data["sub_sector"], "Healthtech")

    def test_the_failure_is_reported_not_silent(self):
        """It was logged at DEBUG, so nothing in the run said what had
        happened until a section went missing."""
        with self.assertLogs("fundos.profile", level="WARNING") as captured:
            self._save_with_broken_taxonomy()
        self.assertTrue(any("TAXONOMY" in line for line in captured.output))

    def test_the_write_is_wrapped_in_a_savepoint(self):
        import inspect

        from fundos.profile import section_writer

        source = inspect.getsource(section_writer._mirror_company_profile)
        self.assertIn("transaction.atomic()", source)
