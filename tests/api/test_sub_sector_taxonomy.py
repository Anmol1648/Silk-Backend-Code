"""`sub_sector` is a join key, not prose.

It selects the peer cohort a company is scored against. A value that is not in
the benchmark table matches no peers, and the benchmark category — a fifth of
the rating — scores blank:

    'B2B E-commerce platform'  ->  unresolved, 3 benchmarks
    'B2B Ecommerce'            ->  exact,      6 benchmarks

Tyreplex stored the first and lost half its benchmarks to a hyphen and a
trailing noun. Its own run said so, in a warning nobody was reading:

    sub_sector_method: 'unresolved', benchmarks: 0
    Category F carries 20% of the rating and will score blank

Meanwhile the prompt offered "e.g. SaaS, Digital Health, Payments" as
guidance. Two of those three are not benchmark groups either, so the guidance
was steering the model off the only list that matters.
"""
from datetime import date
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from fundos.profile import schema


def load_benchmark_groups():
    """A minimal benchmark table, so these tests test something.

    Seeding does not load `SectorDealData` — it comes from an imported
    workbook — so relying on it meant 17 of these 20 skipped, which is the
    same as not having written them. These are the real group names and
    shapes, taken from the loaded table.
    """
    from fundos.assessment.models import SectorDealData

    # The sector level is looked up too, and `resolve_sector("E-commerce")`
    # canonicalises to "Ecommerce" — without this row the cohort resolves and
    # still returns nothing.
    rows = [("sector", "Ecommerce")] + [
        ("sub_sector", name) for name in
        ("B2B Ecommerce", "B2C Ecommerce & Marketplaces", "Healthtech",
         "Vertical SaaS", "Travel Tech SaaS", "Payments", "Unassigned")]
    for level, name in rows:
        SectorDealData.objects.get_or_create(
            level=level, name=name,
            defaults={"clubbed_group": name, "deal_count": 12,
                      "sample_quality": "OK",
                      # The three scores ARE the benchmarks. Without them the
                      # row resolves and still contributes nothing, which is
                      # the same shape of quiet failure these tests exist for.
                      "total_raised_usd_mn": Decimal("724.07"),
                      "avg_ticket_usd_mn": Decimal("16.84"),
                      "investor_count": 23,
                      "velocity_score": Decimal("6.66"),
                      "ticket_score": Decimal("8.12"),
                      "investors_score": Decimal("8.54"),
                      "as_of_date": date(2026, 9, 9),
                      "source_file": "test_fixture.json"})


class TheVocabularyComesFromTheBenchmarkTable(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        load_benchmark_groups()

    def test_the_groups_that_carry_benchmarks_are_offered(self):
        self.assertIn("B2B Ecommerce", schema.sub_sector_vocabulary())

    def test_the_unassigned_bucket_is_not_offered_as_a_choice(self):
        """"Unassigned" is where unmatched rows land, not something to pick."""
        self.assertNotIn("Unassigned", schema.sub_sector_vocabulary())

    def test_an_empty_table_offers_nothing_rather_than_failing(self):
        from fundos.assessment.models import SectorDealData

        SectorDealData.objects.all().delete()
        self.assertEqual(schema.sub_sector_vocabulary(), [])
        self.assertEqual(schema.taxonomy_block(), "",
                         "an empty list must not be put in front of the model")


class TheModelIsShownTheList(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        load_benchmark_groups()

    def test_the_prompt_carries_the_real_groups(self):
        block = schema.schema_prompt_block()
        self.assertIn("B2B Ecommerce", block)
        self.assertIn("MUST be copied", block)

    def test_the_prompt_says_what_it_costs_to_miss(self):
        self.assertIn("scores blank", schema.taxonomy_block())

    def test_the_field_spec_no_longer_suggests_values_that_do_not_resolve(self):
        """"Digital Health" is not a benchmark group; offering it as an
        example steered the model away from the list."""
        spec = schema.sections_by_key().get("company_profile") or {}
        description = (spec.get("fields") or {}).get("sub_sector", "")
        self.assertNotIn("Digital Health", description)


class ANearMissIsRescuedAndAVagueOneIsNot(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        load_benchmark_groups()

    def test_the_reported_defect_resolves(self):
        self.assertEqual(
            schema.canonical_sub_sector("B2B E-commerce platform"),
            "B2B Ecommerce")

    def test_an_exact_value_passes_through(self):
        self.assertEqual(schema.canonical_sub_sector("B2B Ecommerce"),
                         "B2B Ecommerce")

    def test_a_more_specific_claim_resolves_to_its_group(self):
        self.assertEqual(schema.canonical_sub_sector("Vertical SaaS platform"),
                         "Vertical SaaS")

    def test_a_vaguer_claim_is_left_unresolved(self):
        """A company calling itself "SaaS" once resolved to "Travel Tech
        SaaS" — a real cohort, and the wrong industry, benchmarked with every
        appearance of being right. Unresolved is a visible gap; wrongly
        resolved is a lie that survives review."""
        self.assertEqual(schema.canonical_sub_sector("SaaS"), "")

    def test_something_in_no_industry_at_all_resolves_to_nothing(self):
        self.assertEqual(schema.canonical_sub_sector("Underwater Basketry"),
                         "")

    def test_nothing_in_resolves_to_nothing(self):
        for empty in ("", None, "   "):
            self.assertEqual(schema.canonical_sub_sector(empty), "")


class TheProfileIsCanonicalisedAtTheBoundary(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        load_benchmark_groups()

    def _normalize(self, sub_sector, notes=None):
        raw = {"sections": {"company_profile": {
            "data": {"sub_sector": sub_sector, "website": "x"}}}}
        out = schema.normalize_profile(raw, notes=notes)
        return out["sections"]["company_profile"]["data"]["sub_sector"]

    def test_a_near_miss_is_stored_as_its_benchmark_group(self):
        self.assertEqual(self._normalize("B2B E-commerce platform"),
                         "B2B Ecommerce")

    def test_the_change_is_reported_to_the_run(self):
        notes = []
        self._normalize("B2B E-commerce platform", notes)
        self.assertTrue(any("resolved to the benchmark group" in n
                            for n in notes), notes)

    def test_an_unresolvable_value_keeps_the_models_own_words(self):
        """Overwriting it with a plausible neighbour would benchmark the
        company against the wrong industry while looking correct."""
        self.assertEqual(self._normalize("Underwater Basketry"),
                         "Underwater Basketry")

    def test_an_unresolvable_value_is_reported_as_a_gap(self):
        notes = []
        self._normalize("Underwater Basketry", notes)
        self.assertTrue(any("matches no benchmark group" in n for n in notes),
                        notes)

    def test_a_blank_sub_sector_is_left_alone(self):
        self.assertEqual(self._normalize(""), "")


class TheBenchmarksActuallyArrive(TestCase):
    """The point of all of the above."""

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        load_benchmark_groups()

    def test_the_canonical_value_resolves_to_a_cohort(self):
        from fundos.profile.assessment_extraction import resolve_sub_sector

        group, method = resolve_sub_sector("B2B Ecommerce")
        self.assertEqual(group, "B2B Ecommerce")
        # Which internal path matched is not the point — whether a cohort was
        # found is. "unresolved" is the only answer that costs a category.
        self.assertNotEqual(method, "unresolved")

    def test_canonicalising_turns_an_unresolved_value_into_a_cohort(self):
        from fundos.profile.assessment_extraction import resolve_sub_sector

        raw = "B2B E-commerce platform"
        self.assertEqual(resolve_sub_sector(raw)[1], "unresolved")
        fixed = schema.canonical_sub_sector(raw)
        self.assertNotEqual(resolve_sub_sector(fixed)[1], "unresolved")

    def test_more_benchmarks_arrive_for_the_canonical_value(self):
        """The percentile rows come from `ConfigParameter` entries with
        scoring_type="lookup", which arrive with the assessment workbook and
        are not seeded. Without them both sides are legitimately zero, so the
        comparison would pass or fail for the wrong reason."""
        from fundos.assessment.models import ConfigParameter
        from fundos.profile.assessment_extraction import benchmark_inputs

        if not ConfigParameter.objects.filter(is_active=True,
                                              scoring_type="lookup").exists():
            self.skipTest("the assessment workbook is not imported here")

        loose, _, _ = benchmark_inputs("E-commerce", "B2B E-commerce platform")
        exact, group, method = benchmark_inputs("E-commerce", "B2B Ecommerce")
        self.assertNotEqual(method, "unresolved")
        self.assertEqual(group, "B2B Ecommerce")
        self.assertGreater(len(exact), len(loose),
                           "the canonical value must bring more peers")

class TheAssessmentIsHandedWhatTheProfileResolved(TestCase):
    """One run logged both of these, a minute apart:

        "sub-sector 'Healthtech' matched the benchmark group 'Healthtech'"
        sub_sector_method="unresolved" sector="" subSector=""

    The profile resolved it and stored it on the company_profile section.
    The assessment then looked at the model's own answer, and at
    `Company.sector` / `Company.sub_sector` -- two columns nothing in the
    pipeline writes. Category F is a fifth of the rating and scored blank on
    a company whose cohort was known.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_platform_config", verbosity=0)

    def setUp(self):
        import uuid

        from fundos.core.models import Company, Membership, Tenant, User
        from fundos.profile.services import get_or_create_profile

        tenant = Tenant.objects.create(name=f"T-{uuid.uuid4().hex[:8]}")
        self.user = User.objects.create_user(
            email=f"{uuid.uuid4().hex[:8]}@example.com", tenant_id=tenant.id,
            name="Sector User")
        self.company = Company.objects.create(
            tenant_id=tenant.id, name="Cohort Co", created_by=self.user)
        Membership.objects.create(
            tenant_id=tenant.id, user=self.user, scope_type="company",
            scope_id=self.company.id, role="founder", status="active")
        self.profile = get_or_create_profile(self.company, user=self.user)

    def _store(self, macro, sub):
        from fundos.profile.section_writer import update_section_from_data

        update_section_from_data(
            self.profile, "company_profile",
            {"description_of_business": "Care.", "website": "",
             "country": "IN", "macro_sector": macro, "sub_sector": sub,
             "funding_status_name": "", "revenue_size_name": "",
             "currency_id": "INR"},
            user=self.user)

    def _read(self):
        from fundos.profile.assessment_extraction import _sector_from_profile

        return _sector_from_profile(self.profile)

    def test_the_resolved_sub_sector_is_readable_from_the_profile(self):
        self._store("Healthcare", "Healthtech")
        self.assertEqual(self._read(), ("Healthcare", "Healthtech"))

    def test_a_profile_that_says_nothing_returns_blanks(self):
        self.assertEqual(self._read(), ("", ""))

    def test_the_company_row_is_not_where_this_lives(self):
        """Nothing in the pipeline writes those columns, which is exactly why
        reading them found nothing."""
        self._store("Healthcare", "Healthtech")
        self.company.refresh_from_db()
        self.assertFalse(getattr(self.company, "sub_sector", "") or "")

    def test_an_unreadable_profile_is_not_an_error(self):
        """A sector lookup must never be what fails a generation run."""
        from unittest import mock

        with mock.patch("fundos.profile.spec_serializer.serialize_section",
                        side_effect=RuntimeError("boom")):
            self.assertEqual(self._read(), ("", ""))
