"""Three things a live run showed, once the earlier fixes were deployed.

1. The blank-row trimming fired on decks and did NOTHING to a spreadsheet —
   the file it was written for. MarkItDown writes "NaN" into an empty cell
   and "Unnamed: 7" over an unnamed column, and the first version only
   matched a truly blank cell. A 552,541-character financial model stayed
   552,541 characters, 70% of the dossier, mostly scaffolding.

2. Two sub-sector resolvers gave two answers for one string in one run. The
   profile logged "'Digital Health' matches no benchmark group, so the
   benchmark category will score blank"; the assessment resolved it exactly
   to Healthtech a minute later, through an alias table the profile resolver
   never read. The warning shown to the operator was the false one.

3. A profile deleted mid-run — which is what happens when somebody
   re-uploads their documents — was discovered at the save, thirteen minutes
   and one 300,000-token call later, and reported as a foreign-key
   violation naming a UUID.
"""
import uuid

from django.core.management import call_command
from django.test import TestCase


class SpreadsheetScaffoldingIsNotContent(TestCase):

    def _trim(self, text):
        from fundos.profile.pipeline.source2_documents import _drop_empty_rows

        return _drop_empty_rows(text)

    #: What MarkItDown actually emits: NaN cells, invented column headers.
    SHEET = "\n".join([
        "## Summary",
        "| FY | Revenue | Unnamed: 2 | Unnamed: 3 |",
        "| --- | --- | --- | --- |",
        "| FY24 | 56.9 | NaN | NaN |",
        "| NaN | NaN | NaN | NaN |",
        "| NaN | NaN | NaN | NaN |",
        "| FY25 | 415.8 | NaN | NaN |",
    ])

    def test_the_figures_survive(self):
        out = self._trim(self.SHEET)
        self.assertIn("56.9", out)
        self.assertIn("415.8", out)
        self.assertIn("FY24", out)

    def test_nan_rows_are_gone(self):
        self.assertNotIn("| NaN | NaN | NaN | NaN |", self._trim(self.SHEET))

    def test_invented_empty_columns_are_gone(self):
        self.assertNotIn("Unnamed", self._trim(self.SHEET))

    def test_a_named_column_is_kept_even_when_empty(self):
        """A header somebody typed is content: it says what was tracked."""
        sheet = "\n".join(["| FY | Churn |", "| --- | --- |",
                           "| FY24 | NaN |"])
        self.assertIn("Churn", self._trim(sheet))

    def test_an_invented_column_with_data_is_kept(self):
        """Unnamed does not mean empty. A stray figure is still a figure."""
        sheet = "\n".join(["| FY | Unnamed: 1 |", "| --- | --- |",
                           "| FY24 | 56.9 |"])
        self.assertIn("56.9", self._trim(sheet))

    def test_a_row_with_one_figure_survives(self):
        sheet = "\n".join(["| A | B | C |", "| --- | --- | --- |",
                           "| NaN | NaN | 56.9 |"])
        self.assertIn("56.9", self._trim(sheet))

    def test_the_table_still_renders_as_a_table(self):
        out = self._trim(self.SHEET)
        self.assertIn("| --- |", out)
        self.assertTrue(out.startswith("## Summary"))

    def test_a_table_of_nothing_but_scaffolding_is_dropped_whole(self):
        sheet = "\n".join(["| Unnamed: 0 | Unnamed: 1 |", "| --- | --- |",
                           "| NaN | NaN |", "| NaN | NaN |"])
        self.assertEqual(self._trim(sheet).strip(), "")

    def test_prose_between_tables_is_untouched(self):
        text = f"Intro line.\n\n{self.SHEET}\n\nClosing line."
        out = self._trim(text)
        self.assertIn("Intro line.", out)
        self.assertIn("Closing line.", out)

    def test_text_with_no_table_is_returned_unchanged(self):
        self.assertEqual(self._trim("plain prose"), "plain prose")

    def test_it_shrinks_a_real_sheet_substantially(self):
        rows = ["| FY | Revenue | " + " | ".join(
            f"Unnamed: {i}" for i in range(2, 20)) + " |",
            "| " + " | ".join("---" for _ in range(20)) + " |"]
        rows += ["| " + " | ".join("NaN" for _ in range(20)) + " |"] * 200
        rows.append("| FY24 | 56.9 | " + " | ".join(
            "NaN" for _ in range(18)) + " |")
        sheet = "\n".join(rows)
        out = self._trim(sheet)
        self.assertLess(len(out), len(sheet) / 20)
        self.assertIn("56.9", out)

    def test_the_other_empty_spellings_count_too(self):
        for word in ("NaN", "nan", "NaT", "None", "-", ""):
            sheet = "\n".join(["| A | B |", "| --- | --- |",
                               f"| {word} | {word} |", "| FY24 | 56.9 |"])
            out = self._trim(sheet)
            self.assertIn("56.9", out, word)
            self.assertEqual(out.count("FY24"), 1, word)


class OneSubSectorResolver(TestCase):
    """Two resolvers, one string, two answers — and the operator was shown
    the wrong one."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        call_command("seed_initial_data", verbosity=0)

    def setUp(self):
        from fundos.assessment.models import SectorDealData, SectorMapping

        SectorDealData.objects.get_or_create(
            level="sub_sector", name="Healthtech",
            defaults={"clubbed_group": "Healthtech", "deal_count": 10,
                      "sample_quality": "OK", "source_file": "t.json"})
        SectorMapping.objects.get_or_create(
            raw_label="Digital Health",
            defaults={"clubbed_group": "Healthtech"})

    def _profile_side(self, label):
        from fundos.profile.schema import canonical_sub_sector

        return canonical_sub_sector(label)

    def _assessment_side(self, label):
        from fundos.profile.assessment_extraction import resolve_sub_sector

        return resolve_sub_sector(label)[0]

    def test_the_reported_disagreement_is_gone(self):
        self.assertEqual(self._profile_side("Digital Health"), "Healthtech")
        self.assertEqual(self._assessment_side("Digital Health"),
                         "Healthtech")

    def test_both_sides_agree_on_an_alias(self):
        self.assertEqual(self._profile_side("Digital Health"),
                         self._assessment_side("Digital Health"))

    def test_a_group_name_still_resolves_to_itself(self):
        self.assertEqual(self._profile_side("Healthtech"), "Healthtech")

    def test_something_nobody_mapped_still_resolves_to_nothing(self):
        """An unresolved sub-sector is a visible gap; a wrongly resolved one
        benchmarks a company against the wrong industry and looks fine."""
        self.assertEqual(self._profile_side("Underwater Basketry"), "")

    def test_a_missing_alias_table_is_not_an_error(self):
        from unittest import mock

        with mock.patch("fundos.assessment.models.SectorMapping.objects")\
                as objects:
            objects.filter.side_effect = RuntimeError("no table")
            self.assertEqual(self._profile_side("Underwater Basketry"), "")


class ARunWhoseProfileIsDeletedStopsSpending(TestCase):

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
            name="Deleting User")
        self.company = Company.objects.create(
            tenant_id=tenant.id, name="Vanishing Co", created_by=self.user)
        Membership.objects.create(
            tenant_id=tenant.id, user=self.user, scope_type="company",
            scope_id=self.company.id, role="founder", status="active")
        self.profile = get_or_create_profile(self.company, user=self.user)

    def test_a_live_profile_passes_the_check(self):
        from fundos.profile.pipeline.orchestrator import _require_profile

        self.assertIsNone(_require_profile(self.profile))

    def test_a_deleted_profile_stops_the_run(self):
        from fundos.profile.pipeline.orchestrator import (ProfileGone,
                                                          _require_profile)

        self.profile.__class__.objects.filter(pk=self.profile.pk).delete()
        with self.assertRaises(ProfileGone):
            _require_profile(self.profile)

    def test_the_reason_is_in_words_not_a_foreign_key(self):
        from fundos.profile.pipeline.orchestrator import (ProfileGone,
                                                          _require_profile)

        self.profile.__class__.objects.filter(pk=self.profile.pk).delete()
        with self.assertRaises(ProfileGone) as caught:
            _require_profile(self.profile)
        message = str(caught.exception)
        self.assertIn("deleted while the run was in progress", message)
        self.assertIn("nothing has been saved", message)
        self.assertNotIn("foreign key", message.lower())

    def test_the_check_runs_before_the_expensive_call(self):
        """Synthesis is the one big spend. Discovering the deletion after it
        cost thirteen minutes and a 300,000-token call for nothing."""
        import inspect

        from fundos.profile.pipeline import orchestrator

        source = inspect.getsource(orchestrator.run_pipeline)
        guard = source.index("_require_profile")
        synthesis = source.index("synthesize.run")
        self.assertLess(guard, synthesis)

    def test_it_runs_again_before_the_write(self):
        """Synthesis takes minutes, and the profile can go during them."""
        import inspect

        from fundos.profile.pipeline import orchestrator

        source = inspect.getsource(orchestrator.run_pipeline)
        self.assertEqual(source.count("_require_profile(profile, tracker_)"),
                         2)
