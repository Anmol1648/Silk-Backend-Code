"""E.2/E.3/E.6 and F.2/F.3/F.6 — the deal-table percentiles reach the score.

Thirty percent of the rating is scored against the Sector Deal Data table,
and until now those six parameters were resolved in exactly ONE place: inside
step 1's profile extraction. The table is data — the documented way to load
it, and to refresh it, is a re-import — so any assessment extracted before an
import kept six blank rows while the table held every number they needed.
That is what these tests hold shut.

The other half is just as important: when the number genuinely is not there,
the row must stay blank AND say which of the four things went wrong. A cohort
below the sample floor and a sheet nobody imported are not the same problem,
and reporting both as "no percentile is held for this cohort" told a reader
the market data does not exist when it was sitting in the database.
"""
import datetime as dt
from decimal import Decimal

from django.core.management import call_command
from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base

PCTL_KEYS = ["PCTL_E_2", "PCTL_E_3", "PCTL_E_6",
             "PCTL_F_2", "PCTL_F_3", "PCTL_F_6"]
REFS = {"PCTL_E_2": "E.2", "PCTL_E_3": "E.3", "PCTL_E_6": "E.6",
        "PCTL_F_2": "F.2", "PCTL_F_3": "F.3", "PCTL_F_6": "F.6"}


def key_for(ref):
    """The single config input behind one percentile terminal."""
    return next(k for k, r in REFS.items() if r == ref)


class BenchmarkBase(Phase1Base):
    """A company whose cohort IS in the table — the ordinary case."""

    SECTOR = "Healthtech"
    SUB_SECTOR = "Healthtech"

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import SectorDealData

        SectorDealData.objects.all().delete()
        self.sector_row = SectorDealData.objects.create(
            level="sector", name="Healthtech", deal_count=178,
            velocity_score=Decimal("7.50"), ticket_score=Decimal("5.62"),
            investors_score=Decimal("7.50"), as_of_date=dt.date(2026, 9, 1))
        self.sub_row = SectorDealData.objects.create(
            level="sub_sector", name="Healthtech",
            clubbed_group="Healthtech", deal_count=53,
            velocity_score=Decimal("7.50"), ticket_score=Decimal("6.87"),
            investors_score=Decimal("6.66"), as_of_date=dt.date(2026, 9, 1))
        self.assessment.sector = self.SECTOR
        self.assessment.sub_sector = self.SUB_SECTOR
        self.assessment.save(update_fields=["sector", "sub_sector"])

    def payload(self):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def terminals(self, data=None):
        data = data if data is not None else self.payload()
        out = {}

        def walk(node):
            if node.get("isTerminal"):
                out[node["ref"]] = node
            for child in (node.get("subitems") or node.get("children")
                          or []):
                walk(child)

        for cat in data["categories"]:
            walk(cat)
        return out

    def values(self):
        from fundos.assessment.models import ParameterValue
        return {pv.input_key: pv for pv in ParameterValue.objects.filter(
            assessment=self.assessment, input_key__in=PCTL_KEYS)}


class TheTableReachesTheAssessment(BenchmarkBase):

    def test_the_defect_reproduces_without_the_refresh(self):
        """Nothing wrote them: the fixture starts exactly where Zyla was."""
        self.assertEqual(self.values(), {})
        for ref in REFS.values():
            self.assertIsNone(self.terminals()[ref]["score"])

    def test_refreshing_writes_all_six_from_the_current_table(self):
        from fundos.assessment.profile_bridge import refresh_benchmarks

        self.assertEqual(refresh_benchmarks(self.assessment), 6)
        stored = self.values()
        self.assertEqual(sorted(stored), sorted(PCTL_KEYS))
        self.assertEqual(stored["PCTL_F_2"].raw_value, "7.50")
        self.assertEqual(stored["PCTL_F_3"].raw_value, "6.87")
        self.assertEqual(stored["PCTL_F_6"].raw_value, "6.66")
        # Its provenance names the cohort and the sample it was ranked in.
        self.assertIn("sub_sector 'Healthtech'",
                      stored["PCTL_F_2"].source_detail)
        self.assertIn("53 deals", stored["PCTL_F_2"].source_detail)
        self.assertEqual(stored["PCTL_F_2"].source_tier, 2)

    def test_the_terminals_score_once_the_rows_exist(self):
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.assessment.services import run_scoring

        refresh_benchmarks(self.assessment)
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

        terms = self.terminals()
        for key, ref in REFS.items():
            node = terms[ref]
            self.assertIsNotNone(node["score"], f"{ref} still unscored")
            self.assertIsNotNone(node["band"], ref)
            self.assertEqual(node["status"], "scored", ref)
            self.assertEqual(node["key"], key)
            self.assertFalse(node["weight"]["excluded"], ref)

        self.assertEqual(terms["F.2"]["value"]["raw"], 7.5)
        self.assertEqual(terms["F.3"]["value"]["raw"], 6.87)
        self.assertEqual(terms["F.6"]["value"]["raw"], 6.66)

    def test_a_scored_percentile_traces_the_banding_it_went_through(self):
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.assessment.services import run_scoring

        refresh_benchmarks(self.assessment)
        run_scoring(self.assessment)
        trace = self.terminals()["F.6"]["trace"]
        self.assertEqual(trace["method"], "lookup")
        self.assertEqual(trace["unit"], "percentile")
        self.assertIsNotNone(trace["matched_band"])
        self.assertTrue(trace["thresholds"])

    def test_category_f_scores_where_it_could_not_before(self):
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.assessment.services import run_scoring

        before = {c["code"]: c["score"] for c in self.payload()["categories"]}
        self.assertIsNone(before["F"])

        refresh_benchmarks(self.assessment)
        run_scoring(self.assessment)
        after = {c["code"]: c["score"] for c in self.payload()["categories"]}
        self.assertIsNotNone(after["F"], "category F still cannot score")
        self.assertIsNotNone(after["E"])

    def test_the_bridge_refreshes_them_on_every_seed(self):
        """The path a real generation takes, not just the command."""
        from fundos.assessment.profile_bridge import seed_from_profile

        counts = seed_from_profile(self.assessment)
        self.assertEqual(counts.get("benchmarks"), 6)
        self.assertEqual(sorted(self.values()), sorted(PCTL_KEYS))

    def test_a_later_re_import_moves_the_stored_value(self):
        """A refresh is a re-import — the whole point of storing the table."""
        from fundos.assessment.profile_bridge import refresh_benchmarks

        refresh_benchmarks(self.assessment)
        self.assertEqual(self.values()["PCTL_F_3"].raw_value, "6.87")

        self.sub_row.ticket_score = Decimal("9.10")
        self.sub_row.save(update_fields=["ticket_score"])
        self.assertEqual(refresh_benchmarks(self.assessment), 6)
        self.assertEqual(self.values()["PCTL_F_3"].raw_value, "9.10")

    def test_a_reviewer_override_is_never_overwritten(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.profile_bridge import refresh_benchmarks

        ParameterValue.objects.create(
            assessment=self.assessment, input_key="PCTL_F_2",
            raw_value="2.00", category="F", ref_code="F.2",
            is_overridden=True, source_type="founder", source_tier=1,
            confidence=Decimal("1.0"))
        self.assertEqual(refresh_benchmarks(self.assessment), 5)
        self.assertEqual(self.values()["PCTL_F_2"].raw_value, "2.00")


class MissingStaysMissingAndSaysWhy(BenchmarkBase):

    def test_an_unimported_table_says_so_rather_than_denying_the_cohort(self):
        from fundos.assessment.models import SectorDealData
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.profile.assessment_extraction import (
            BENCHMARK_TABLE_EMPTY, benchmark_absences)

        SectorDealData.objects.all().delete()
        self.assertEqual(refresh_benchmarks(self.assessment), 0)

        absences = benchmark_absences(self.SECTOR, self.SUB_SECTOR)
        self.assertEqual(sorted(absences), sorted(PCTL_KEYS))
        for key in PCTL_KEYS:
            reason, sentence = absences[key]
            self.assertEqual(reason, BENCHMARK_TABLE_EMPTY)
            self.assertIn("has not been imported", sentence)

        node = self.terminals()["F.2"]
        self.assertIsNone(node["score"])
        self.assertEqual(node["trace"]["reason"], BENCHMARK_TABLE_EMPTY)
        self.assertIn("has not been imported", node["trace"]["explanation"])

    def test_an_unresolved_sub_sector_blanks_only_the_f_side(self):
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.assessment.services import run_scoring
        from fundos.profile.assessment_extraction import BENCHMARK_UNRESOLVED

        self.assessment.sub_sector = "Something Nobody Mapped"
        self.assessment.save(update_fields=["sub_sector"])
        self.assertEqual(refresh_benchmarks(self.assessment), 3)
        run_scoring(self.assessment)

        terms = self.terminals()
        for ref in ("E.2", "E.3", "E.6"):
            self.assertIsNotNone(terms[ref]["score"], ref)
        for ref in ("F.2", "F.3", "F.6"):
            self.assertIsNone(terms[ref]["score"], ref)
            self.assertEqual(terms[ref]["trace"]["reason"],
                             BENCHMARK_UNRESOLVED)
            self.assertIn("could not be matched",
                          terms[ref]["trace"]["explanation"])

    def test_a_cohort_below_the_sample_floor_is_withheld_not_guessed(self):
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.profile.assessment_extraction import (
            BENCHMARK_BELOW_FLOOR, benchmark_absences)

        # The sheet withholds a percentile it has too few deals to rank.
        self.sub_row.deal_count = 3
        self.sub_row.velocity_score = None
        self.sub_row.ticket_score = None
        self.sub_row.investors_score = None
        self.sub_row.save()

        self.assertEqual(refresh_benchmarks(self.assessment), 3)
        self.assertEqual(sorted(k for k in self.values()),
                         ["PCTL_E_2", "PCTL_E_3", "PCTL_E_6"])

        absences = benchmark_absences(self.SECTOR, self.SUB_SECTOR)
        for key in ("PCTL_F_2", "PCTL_F_3", "PCTL_F_6"):
            reason, sentence = absences[key]
            self.assertEqual(reason, BENCHMARK_BELOW_FLOOR)
            self.assertIn("3 deals", sentence)
            self.assertIn("reliable-sample floor", sentence)

        node = self.terminals()["F.3"]
        self.assertIsNone(node["score"])
        self.assertIsNone(node["value"]["raw"])
        self.assertEqual(node["trace"]["reason"], BENCHMARK_BELOW_FLOOR)

    def test_a_cohort_absent_from_the_table_names_the_label_that_missed(self):
        """The label maps to a group the imported sheet does not carry.

        Distinct from `cohort_unresolved`: the mapping did its job and the
        answer is that the sheet is missing a row, which is a different
        person's fix.
        """
        from fundos.assessment.models import SectorMapping
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.profile.assessment_extraction import (
            BENCHMARK_COHORT_MISSING, benchmark_absences)

        SectorMapping.objects.create(raw_label="Zyla Health",
                                     clubbed_group="Healthtech")
        self.assessment.sub_sector = "Zyla Health"
        self.assessment.save(update_fields=["sub_sector"])
        self.sub_row.delete()

        self.assertEqual(refresh_benchmarks(self.assessment), 3)
        reason, sentence = benchmark_absences(self.SECTOR,
                                              "Zyla Health")["PCTL_F_6"]
        self.assertEqual(reason, BENCHMARK_COHORT_MISSING)
        self.assertIn("Healthtech", sentence)
        self.assertIn("not a row", sentence)

    def test_a_withheld_percentile_is_never_filled_with_a_number(self):
        """The one thing worse than a blank row is an invented one."""
        from fundos.assessment.models import SectorDealData
        from fundos.assessment.profile_bridge import refresh_benchmarks
        from fundos.assessment.services import run_scoring

        SectorDealData.objects.all().delete()
        refresh_benchmarks(self.assessment)
        run_scoring(self.assessment)

        for ref in REFS.values():
            node = self.terminals()[ref]
            self.assertIsNone(node["score"], ref)
            self.assertIsNone(node["value"]["raw"], ref)
            self.assertEqual(node["evidence"]["tier"], "Not Evidenced", ref)
            self.assertTrue(node["weight"]["excluded"], ref)
            self.assertEqual(node["weight"]["contribution"], 0.0, ref)
            # The parameter still exists and still names the row that would
            # answer it. What must not appear is a value.
            self.assertEqual(node["key"], key_for(ref), ref)
            self.assertNotIn("inputs", node, ref)


class TheRefreshCommand(BenchmarkBase):

    def test_dry_run_reports_without_writing(self):
        call_command("refresh_assessment_benchmarks",
                     company=str(self.company.id), dry_run=True, verbosity=0)
        self.assertEqual(self.values(), {})

    def test_the_command_writes_and_rescores(self):
        call_command("refresh_assessment_benchmarks",
                     company=str(self.company.id), rescore=True, verbosity=0)
        self.assertEqual(sorted(self.values()), sorted(PCTL_KEYS))
        self.assessment.refresh_from_db()
        terms = self.terminals()
        for ref in ("F.2", "F.3", "F.6"):
            self.assertIsNotNone(terms[ref]["score"], ref)

    def test_the_command_is_idempotent(self):
        call_command("refresh_assessment_benchmarks",
                     company=str(self.company.id), verbosity=0)
        first = {k: v.raw_value for k, v in self.values().items()}
        call_command("refresh_assessment_benchmarks",
                     company=str(self.company.id), verbosity=0)
        self.assertEqual({k: v.raw_value for k, v in self.values().items()},
                         first)


class TheOldEntryPointStillWorks(BenchmarkBase):
    """`benchmark_inputs` is called by step 1; its contract is unchanged."""

    def test_it_still_returns_rows_group_and_method(self):
        from fundos.profile.assessment_extraction import benchmark_inputs

        rows, group, method = benchmark_inputs(self.SECTOR, self.SUB_SECTOR)
        self.assertEqual(len(rows), 6)
        self.assertEqual(group, "Healthtech")
        self.assertIn(method, ("exact", "group", "name"))
        self.assertEqual({r["input_key"] for r in rows}, set(PCTL_KEYS))
