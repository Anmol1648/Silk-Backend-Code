"""v24 — the workbook is the configuration, and Stage 1 must be grounded.

Every defect this suite pins down had one cause: a SECOND COPY of the truth.
The scoring model existed in the workbook and again in Python, and the two
were free to drift. They did — sub-sector keys spelled differently, six
parameters invented, percentiles scored as their own value, thin samples
scored rather than withheld.

The tests below therefore assert against the workbook itself wherever it is
available, rather than against a transcription of it.
"""
import os
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

# Point FUNDOS_ASSESSMENT_WORKBOOK at a filled assessment workbook to run the
# tests that read it. The workbook is a customer artefact and is not committed,
# so these skip by default rather than depending on one machine's filesystem.
WORKBOOK = os.environ.get("FUNDOS_ASSESSMENT_WORKBOOK", "")
HAVE_WB = bool(WORKBOOK) and os.path.exists(WORKBOOK)
skip_reason = ("the assessment workbook is not present — set "
               "FUNDOS_ASSESSMENT_WORKBOOK to its path to run these")


def _company(name="Any Co"):
    from fundos.core.models import Company, Tenant
    t = Tenant.objects.create(name="T")
    return t, Company.objects.create(tenant_id=t.id, name=name)


# ===========================================================================
# Reading the workbook
# ===========================================================================

class WorkbookParseTests(TestCase):

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)

    def test_the_input_contract_is_exactly_eighty_rows(self):
        """The sheet counts coverage as 'Inputs Captured (Of 80)'."""
        from fundos.assessment.workbook import parse_config_workbook
        data = parse_config_workbook(WORKBOOK)
        self.assertEqual(data["report"]["inputs"], 80)

    def test_sub_sector_keys_carry_the_suffix_the_sheet_uses(self):
        """`SEC_TAM_SUB`, not `SUB_TAM`.

        The suffix is load-bearing: the band formula on every input row opens
        with LET(key, SUBSTITUTE($C22,"_SUB",""), …) to find the rubric. A key
        spelled the other way matches no parameter and no rubric.
        """
        from fundos.assessment.workbook import parse_config_workbook
        keys = {i["input_key"]
                for i in parse_config_workbook(WORKBOOK)["inputs"]}
        for key in ("SEC_TAM_SUB", "SEC_TAM_CAGR_SUB", "SEC_PEER_AGE_SUB",
                    "SEC_PEER_RAISE_MTHS_SUB"):
            self.assertIn(key, keys)
        for invented in ("SUB_TAM", "SUB_VELOCITY", "SEC_VELOCITY"):
            self.assertNotIn(invented, keys)

    def test_constants_are_read_rather_than_hardcoded(self):
        from fundos.assessment.workbook import parse_config_workbook
        c = parse_config_workbook(WORKBOOK)["report"]["constants"]
        self.assertEqual(c["score_excellent"], 9)
        self.assertEqual(c["score_poor"], 3)
        self.assertEqual(c["percentile_excellent"], 8)
        self.assertEqual(c["percentile_fair"], 4)
        self.assertEqual(c["min_deals_for_percentile"], 5)
        self.assertEqual(c["rating_very_good"], 7)

    def test_the_six_percentile_nodes_are_found_from_their_formulae(self):
        """Detected from the Value formula, not the row label.

        The label is prose; the column reference is what actually decides
        which number scores. A values-only read cannot see it at all, which
        is how the first attempt found zero of them on a blank template.
        """
        from fundos.assessment.workbook import parse_config_workbook
        leaves = parse_config_workbook(WORKBOOK)["leaves"]
        pct = {lf["node"]: (lf["benchmark_level"], lf["benchmark_metric"])
               for lf in leaves if lf.get("benchmark_metric")}
        self.assertEqual(pct, {
            "E.2": ("sector", "velocity"), "E.3": ("sector", "ticket"),
            "E.6": ("sector", "investors"),
            "F.2": ("sub_sector", "velocity"),
            "F.3": ("sub_sector", "ticket"),
            "F.6": ("sub_sector", "investors")})

    def test_category_weights_come_from_the_sheet_and_sum_to_100(self):
        from fundos.assessment.workbook import parse_config_workbook
        cats = parse_config_workbook(WORKBOOK)["categories"]
        weights = {c["code"]: float(c["weight"]) for c in cats}
        self.assertEqual(weights,
                         {"A": 20, "B": 20, "C": 10, "D": 10, "E": 10,
                          "F": 20, "G": 10})

    def test_the_data_dictionary_supplies_extraction_guidance(self):
        """The sheet already says what to do when a value is missing, and it
        is not what anyone would have guessed."""
        from fundos.assessment.workbook import parse_config_workbook
        d = parse_config_workbook(WORKBOOK)["dictionary"]
        self.assertGreater(len(d), 60)
        self.assertIn("Not Enough Information", d["A.1.d"]["if_missing"])
        self.assertTrue(d["A.2.a"]["where_to_find"])

    def test_a_structurally_broken_workbook_is_refused(self):
        from fundos.assessment.workbook import WorkbookError, read_inputs

        class FakeSheet:
            def cell(self, r, c):
                class C:
                    value = "DUPLICATE_KEY" if c == 3 else ""
                return C()

        with self.assertRaises(WorkbookError):
            read_inputs({"Company Profile": FakeSheet()})


# ===========================================================================
# Importing it as configuration
# ===========================================================================

class WorkbookImportTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)

    def test_the_stored_contract_is_eighty_collected_rows(self):
        from fundos.assessment.models import ConfigParameter
        self.assertEqual(
            ConfigParameter.objects.filter(
                is_active=True, is_workbook_input=True).count(), 80)

    def test_percentile_nodes_are_marked_as_computed_not_collected(self):
        """Or the sheet's 'of 80' coverage count stops meaning anything."""
        from fundos.assessment.models import ConfigParameter
        rows = ConfigParameter.objects.filter(is_active=True,
                                              scoring_type="lookup")
        self.assertEqual(rows.count(), 6)
        self.assertEqual(rows.filter(is_workbook_input=True).count(), 0)

    def test_sub_keys_inherit_their_base_rubric(self):
        """As the sheet's own SUBSTITUTE(key,"_SUB","") does."""
        from fundos.assessment.models import ConfigRubric
        base = ConfigRubric.objects.get(input_key="SEC_TAM",
                                        stage="Series A", is_active=True)
        sub = ConfigRubric.objects.get(input_key="SEC_TAM_SUB",
                                       stage="Series A", is_active=True)
        self.assertEqual(sub.cut_excellent, base.cut_excellent)
        self.assertEqual(sub.cut_fair, base.cut_fair)

    def test_constants_land_in_config(self):
        from fundos.assessment.services import constant
        self.assertEqual(Decimal(str(constant("score_excellent"))),
                         Decimal("9"))
        self.assertEqual(Decimal(str(constant("min_deals_for_percentile"))),
                         Decimal("5"))

    def test_every_category_has_scoreable_parameters(self):
        from fundos.assessment.models import ConfigParameter
        for code in "ABCDEFG":
            self.assertGreater(
                ConfigParameter.objects.filter(
                    is_active=True, category_code=code,
                    feeds_score=True).count(), 0, f"category {code}")

    def test_reference_rows_are_collected_but_do_not_score(self):
        """The sheet: they "do not feed a score, but they must still be
        filled where the data exists"."""
        from fundos.assessment.models import ConfigParameter
        ref = ConfigParameter.objects.get(input_key="BQ_TOP1_CLIENT")
        self.assertTrue(ref.is_workbook_input)
        self.assertFalse(ref.feeds_score)

    def test_reimport_replaces_rather_than_merges(self):
        """A merge would leave a parameter deleted from the workbook alive in
        config — exactly how the previous copy drifted."""
        from fundos.assessment.models import ConfigParameter
        ConfigParameter.objects.create(
            tenant_id=None, version=1, input_key="STALE_KEY",
            ref_code="Z.9", category_code="Z", name="Stale", is_active=True)
        call_command("import_assessment_workbook", WORKBOOK,
                     config_version=1, activate=True, verbosity=0)
        self.assertFalse(
            ConfigParameter.objects.filter(input_key="STALE_KEY").exists())


# ===========================================================================
# Scoring exactly as the sheet does
# ===========================================================================

class WorkbookScoringTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)
        from fundos.assessment.models import Assessment
        t, c = _company()
        self.a = Assessment.objects.create(company=c, tenant_id=t.id,
                                           deal_stage="Series A")

    def _band(self, key, raw):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import band_parameter
        pv, _ = ParameterValue.objects.update_or_create(
            assessment=self.a, input_key=key,
            defaults={"raw_value": str(raw)})
        return band_parameter(pv, "Series A")

    def test_monotonic_cut_points_match_the_sheet(self):
        """FIN_REV_SCALE at Series A: Exc 75, Good 40, Fair 15 (INR Cr)."""
        self.assertEqual(self._band("FIN_REV_SCALE", 80)[0], "Excellent")
        self.assertEqual(self._band("FIN_REV_SCALE", 41)[0], "Good")
        self.assertEqual(self._band("FIN_REV_SCALE", 20)[0], "Fair")
        self.assertEqual(self._band("FIN_REV_SCALE", 5)[0], "Poor")

    def test_lower_is_better_rows_invert(self):
        """BQ_TOP5_CLIENT at Series A: Exc ≤45, Good ≤65, Fair ≤80."""
        self.assertEqual(self._band("BQ_TOP5_CLIENT", 30)[0], "Excellent")
        self.assertEqual(self._band("BQ_TOP5_CLIENT", 90)[0], "Poor")

    def test_stage_changes_the_band(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import band_parameter
        pv = ParameterValue(assessment=self.a, input_key="TEAM_FDR_EXP",
                            raw_value="7")
        self.assertEqual(band_parameter(pv, "Seed")[0], "Good")
        self.assertEqual(band_parameter(pv, "Growth")[0], "Fair")

    def test_band_scores_come_from_the_scoring_key(self):
        self.assertEqual(self._band("FIN_REV_SCALE", 80)[1], 9.0)
        self.assertEqual(self._band("FIN_REV_SCALE", 41)[1], 7.0)
        self.assertEqual(self._band("FIN_REV_SCALE", 20)[1], 5.0)
        self.assertEqual(self._band("FIN_REV_SCALE", 5)[1], 3.0)

    def test_percentile_is_banded_then_scored(self):
        for raw, band, score in (("10", "Excellent", 9.0),
                                 ("6.5", "Good", 7.0),
                                 ("4", "Fair", 5.0), ("0.3", "Poor", 3.0)):
            self.assertEqual(self._band("PCTL_E_2", raw), (band, score))

    def test_e1_is_scored_on_the_worse_of_tam_and_growth(self):
        """The sheet's own note: a large but stagnant market is not
        attractive, so size must not be able to mask stagnation."""
        from fundos.assessment.models import ParameterValue

        # TAM 14.5 alone is Excellent (cut 10); CAGR 18 is only Good (cut 25).
        ParameterValue.objects.create(assessment=self.a,
                                      input_key="SEC_TAM_CAGR",
                                      raw_value="18")
        self.assertEqual(self._band("SEC_TAM", "14.5")[0], "Good")

    def test_a_blank_input_is_excluded_not_zeroed(self):
        self.assertEqual(self._band("FIN_REV_SCALE", ""), (None, None))

    def test_range_rubric_uses_the_sheet_tolerances(self):
        """BQ_CO_AGE at Series A: ideal 2-6 years, tolerances 25% / 50%."""
        self.assertEqual(self._band("BQ_CO_AGE", 4)[0], "Excellent")
        self.assertEqual(self._band("BQ_CO_AGE", 30)[0], "Poor")


# ===========================================================================
# Benchmarks under the sheet's own sampling rule
# ===========================================================================

class BenchmarkSamplingTests(TestCase):

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)
            call_command("import_sector_benchmarks", WORKBOOK, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)

    def test_the_three_blocks_load(self):
        from fundos.assessment.models import SectorDealData, SectorMapping
        self.assertEqual(
            SectorDealData.objects.filter(level="sector").count(), 18)
        self.assertEqual(
            SectorDealData.objects.filter(level="sub_sector").count(), 33)
        self.assertEqual(SectorMapping.objects.count(), 104)

    def test_a_thin_sample_scores_blank_rather_than_low(self):
        """Web3 has four deals, under the sheet's floor of five, and its three
        score cells are empty in the shipped workbook. Scoring it would credit
        a rank computed against almost nothing."""
        from fundos.assessment.models import SectorDealData
        web3 = SectorDealData.objects.get(level="sector", name="Web3")
        self.assertLess(web3.deal_count, 5)
        self.assertIsNone(web3.velocity_score)
        self.assertIsNone(web3.ticket_score)
        self.assertIsNone(web3.investors_score)

    def test_the_sheets_own_scores_are_preserved(self):
        from fundos.assessment.models import SectorDealData
        fin = SectorDealData.objects.get(level="sector", name="Fintech")
        self.assertEqual(fin.deal_count, 373)
        self.assertEqual(round(float(fin.ticket_score)), 10)

    def test_insurtech_resolves_to_its_own_group(self):
        """The live log reported this label unresolved. It is a clubbed group
        in the sheet with 15 deals — the real cause was an empty table."""
        from fundos.profile.assessment_extraction import resolve_sub_sector
        group, method = resolve_sub_sector("Insurtech")
        self.assertEqual(group, "Insurtech")
        # Either route is a real resolution: the label is both a raw mapping
        # row and a clubbed group. What matters is that it is not unresolved.
        self.assertIn(method, ("exact", "group", "name"))

    def test_benchmark_rows_are_emitted_for_both_populations(self):
        from fundos.profile.assessment_extraction import benchmark_inputs
        rows, group, _m = benchmark_inputs("Fintech", "Insurtech")
        self.assertEqual(group, "Insurtech")
        self.assertEqual({r["input_key"] for r in rows},
                         {"PCTL_E_2", "PCTL_E_3", "PCTL_E_6",
                          "PCTL_F_2", "PCTL_F_3", "PCTL_F_6"})

    def test_a_thin_group_contributes_no_percentile_rows(self):
        from fundos.assessment.models import SectorDealData
        from fundos.profile.assessment_extraction import benchmark_inputs
        SectorDealData.objects.filter(level="sector", name="Fintech").update(
            velocity_score=None, ticket_score=None, investors_score=None)
        rows, _g, _m = benchmark_inputs("Fintech", "Insurtech")
        self.assertEqual({r["input_key"] for r in rows},
                         {"PCTL_F_2", "PCTL_F_3", "PCTL_F_6"})


# ===========================================================================
# Stage 1 — the part that actually blocked testing
# ===========================================================================

class GroundingGuardTests(TestCase):
    """A run with no sources must not produce a confident dossier.

    Both live runs populated hundreds of fields with `searches=""`; one did it
    from two characters of source material. That output reads as authoritative,
    cannot be cited or dated, and is worse on a scorecard than a blank result,
    because a blank result is visibly blank.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)
        from fundos.profile.models import CompanyProfile
        t, c = _company()
        self.profile = CompanyProfile.objects.create(
            tenant_id=t.id, company=c, website_url="https://example.test")

    def test_an_empty_source_bundle_is_refused(self):
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        out = extract_assessment_inputs(self.profile, payloads={})
        self.assertEqual(out["error"], "ungrounded")
        self.assertEqual(out["written"], 0)

    def test_a_two_character_website_is_refused(self):
        """The exact shape of the Onsurity run."""
        from fundos.profile.assessment_extraction import grounding
        ok, why, mode = grounding({"website": {"text": "x"}, "documents": [],
                                   "research": {}})
        self.assertFalse(ok)
        self.assertEqual(mode, "none")
        self.assertIn("nothing was retrieved", why)

    def test_real_sources_pass_the_guard(self):
        from fundos.profile.assessment_extraction import grounding
        # A website alone is now DEGRADED rather than fully accepted: it can
        # evidence company age and named seats, not market size.
        ok, _why, mode = grounding({"website": {"text": "x" * 900}})
        self.assertTrue(ok)
        self.assertEqual(mode, "website_only")
        self.assertEqual(
            grounding({"website": {"text": "x" * 900},
                       "documents": [{"t": "deck"}]})[2], "full")

    def test_a_value_without_a_source_url_is_discarded(self):
        """No URL, no value — the rule that stops a remembered figure
        entering the scorecard wearing a confidence score."""
        from unittest.mock import patch

        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)

        fake = {"values": [
            {"inputKey": "TEAM_FDR_EXP", "value": "12", "unit": "Years",
             "sourceUrl": "", "confidence": 0.95},
            {"inputKey": "BQ_CO_AGE", "value": "7", "unit": "Years",
             "sourceUrl": "https://example.test/about", "confidence": 0.9},
        ], "bands": [], "sector": "Fintech", "subSector": "Insurtech",
            "missing": []}
        with patch("fundos.profile.assessment_extraction._ask",
                   return_value=fake):
            out = extract_assessment_inputs(
                self.profile, payloads={"website": {"text": "x" * 900},
                                        "documents": [{"text": "deck"}]})
        self.assertEqual(out["unsourced_rejected"], 1)
        keys = set(self.profile.assessment_inputs.values_list("input_key",
                                                              flat=True))
        self.assertIn("BQ_CO_AGE", keys)
        self.assertNotIn("TEAM_FDR_EXP", keys)

    def test_the_question_set_is_read_from_config_not_written(self):
        from fundos.profile.assessment_extraction import question_set
        numeric, anchors = question_set()
        keys = {p.input_key for p in numeric} | {p.input_key for p in anchors}
        self.assertIn("SEC_TAM_SUB", keys)
        # Category B is the financial model and G is advisor-internal;
        # sourcing either from research is the failure mode, not the goal.
        self.assertNotIn("FIN_REV_SCALE", keys)
        self.assertNotIn("IB_LIVE_MANDATES", keys)

    def test_no_config_is_reported_rather_than_silently_asking_nothing(self):
        from fundos.assessment.models import ConfigParameter
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        ConfigParameter.objects.update(is_active=False)
        out = extract_assessment_inputs(
            self.profile, payloads={"website": {"text": "x" * 900},
                                    "documents": [{"text": "deck"}]})
        self.assertEqual(out["error"], "no_config")


class EndToEndTests(TestCase):
    """Step 1 collects, the bridge carries, the engine scores."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)
            call_command("import_sector_benchmarks", WORKBOOK, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)

    def test_a_full_run_scores_every_researchable_category(self):
        from fundos.assessment.tasks import generate_assessment
        from fundos.core.models import Deal, Membership, User
        from fundos.core.scoping import tenant_context
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        from fundos.profile.models import CompanyProfile

        t, c = _company("Plum")
        with tenant_context(t.id):
            u = User.objects.create_user(email="f@x.io", tenant_id=t.id,
                                         name="F")
            d = Deal.objects.create(tenant_id=t.id, company=c, name="D1",
                                    round_type="seed", primary_owner=u,
                                    created_by=u)
            Membership.objects.create(tenant_id=t.id, user=u,
                                      scope_type="deal", scope_id=d.id,
                                      role="founder", status="active")
        p = CompanyProfile.objects.create(tenant_id=t.id, company=c,
                                          website_url="https://plumhq.com")
        out = extract_assessment_inputs(
            p, payloads={"website": {"text": "x" * 900},
                         "documents": [{"text": "deck"}]})
        self.assertGreater(out["written"], 10)
        self.assertEqual(out["benchmarks"], 6)
        self.assertNotEqual(out["sub_sector_method"], "unresolved")

        a = generate_assessment(d, user_id=u.id, deal_stage="Series A")
        scores = {cs.category_code: cs.score
                  for cs in a.category_scores.all()}
        for code in ("A", "C", "E", "F"):
            self.assertIsNotNone(scores.get(code),
                                 f"category {code} did not score")
        # B is the financial model, D the deal terms, G the mandate — none of
        # them researchable, so blank here is the correct answer.
        for code in ("B", "D", "G"):
            self.assertIsNone(scores.get(code))

    def test_coverage_is_measured_against_the_eighty_row_contract(self):
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import run_scoring

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="12", category="A")
        run_scoring(a)
        a.refresh_from_db()
        self.assertLess(float(a.input_coverage_pct), 15)


# ===========================================================================
# v25 — Stage 1 repairs, and Stage 2 without a model
# ===========================================================================

class SectorVocabularyTests(TestCase):
    """The label failures from the live log, each one now resolved."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)
            call_command("import_sector_benchmarks", WORKBOOK, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)

    def test_spacing_no_longer_decides_a_twenty_percent_lookup(self):
        """The run returned 'Health Tech'; the sheet says 'Healthtech'."""
        from fundos.profile.assessment_extraction import resolve_sector
        self.assertEqual(resolve_sector("Health Tech"), "Healthtech")
        self.assertEqual(resolve_sector("health  tech"), "Healthtech")

    def test_the_model_is_given_a_closed_list_to_choose_from(self):
        """It returned 'Corporate Employee Health Benefits' — a reasonable
        description matching nothing, because it was never shown the 18
        sectors and 33 groups it was expected to pick from."""
        from fundos.profile.assessment_extraction import vocabulary
        sectors, groups = vocabulary()
        self.assertEqual(len(sectors), 18)
        self.assertEqual(len(groups), 33)
        self.assertIn("Healthtech", sectors)
        self.assertIn("Insurtech", groups)

    def test_the_vocabulary_reaches_the_model(self):
        from unittest.mock import patch

        from fundos.profile.assessment_extraction import _ask, question_set
        from fundos.profile.models import CompanyProfile

        t, c = _company()
        p = CompanyProfile.objects.create(tenant_id=t.id, company=c,
                                          website_url="https://x.test")
        numeric, anchors = question_set()
        with patch("fundos.llm.adapter.llm_generate",
                   return_value={}) as call:
            _ask(p, {"website": {"text": "x"}}, numeric, anchors)
        context = call.call_args.kwargs["context"]
        self.assertEqual(len(context["sector_vocabulary"]), 18)
        self.assertEqual(len(context["sub_sector_vocabulary"]), 33)

    def test_an_unknown_label_still_resolves_to_nothing(self):
        """Loosened matching must not become guessing: a wrong group scores
        20% of the rating against the wrong population."""
        from fundos.profile.assessment_extraction import resolve_sub_sector
        group, method = resolve_sub_sector("Artisanal Cheese Logistics")
        self.assertIsNone(group)
        self.assertEqual(method, "unresolved")


class GroundingByRetrievalTests(TestCase):
    """Character count was the wrong measure.

    Plum cleared the 400-character floor with 6,020 characters of marketing
    website, and the deep extract then populated 229 of 231 fields from it
    with no search. A website does not contain a founder's years in industry
    or a peer's last raise, so those came from recollection — and the gate
    passed them.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)

    def test_a_website_only_run_is_degraded_not_accepted_whole(self):
        from fundos.profile.assessment_extraction import grounding
        ok, why, mode = grounding({"website": {"text": "x" * 6000}})
        self.assertTrue(ok)
        self.assertEqual(mode, "website_only")
        self.assertIn("no search", why)

    def test_retrieval_of_any_kind_gives_a_full_run(self):
        from fundos.profile.assessment_extraction import grounding
        self.assertEqual(
            grounding({"website": {"text": "x" * 500},
                       "documents": [{"text": "deck"}]})[2], "full")
        self.assertEqual(
            grounding({"research": {"news": "y" * 400}})[2], "full")
        self.assertEqual(grounding({}, searched=True)[2], "full")

    def test_nothing_at_all_is_still_refused(self):
        from fundos.profile.assessment_extraction import grounding
        ok, _why, mode = grounding({})
        self.assertFalse(ok)
        self.assertEqual(mode, "none")

    def test_website_only_asks_only_what_a_website_can_evidence(self):
        """Company age and named CXO seats, yes. Market size and peer raise
        recency, no — a marketing page cannot evidence them, and asking is an
        invitation to invent."""
        from unittest.mock import patch

        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        from fundos.profile.models import CompanyProfile

        t, c = _company()
        p = CompanyProfile.objects.create(tenant_id=t.id, company=c,
                                          website_url="https://x.test")
        seen = {}

        def capture(profile, payloads, numeric, anchors, user=None):
            seen["keys"] = ({p.input_key for p in numeric}
                            | {p.input_key for p in anchors})
            return {"values": [], "bands": []}

        with patch("fundos.profile.assessment_extraction._ask", capture):
            out = extract_assessment_inputs(
                p, payloads={"website": {"text": "x" * 6000}})
        self.assertEqual(out["mode"], "website_only")
        self.assertIn("BQ_CO_AGE", seen["keys"])
        self.assertNotIn("SEC_TAM", seen["keys"])
        self.assertNotIn("SEC_PEER_RAISE_MTHS", seen["keys"])


class AnswerAccountingTests(TestCase):
    """Every answer is accounted for.

    The live run reported 61 asked, 35 missing, 1 written — leaving 25
    answers that were neither written nor explained. They hit a silent
    `continue` and no counter recorded them, which is why the log could not
    say what had gone wrong.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)
            call_command("import_sector_benchmarks", WORKBOOK, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)
        from fundos.profile.models import CompanyProfile
        t, c = _company()
        self.profile = CompanyProfile.objects.create(
            tenant_id=t.id, company=c, website_url="https://x.test")

    def _run(self, payload):
        from unittest.mock import patch

        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        with patch("fundos.profile.assessment_extraction._ask",
                   return_value=payload):
            return extract_assessment_inputs(
                self.profile,
                payloads={"website": {"text": "x" * 500},
                          "documents": [{"text": "deck"}]})

    def test_an_anchor_answered_in_values_is_accepted_not_dropped(self):
        """Where a model puts an answer is a formatting detail; whether it
        answered is not."""
        out = self._run({"values": [
            {"inputKey": "ANC_BOARD", "value": "Good",
             "sourceUrl": "https://x.test/board", "confidence": 0.8}],
            "bands": [], "sector": "Fintech", "subSector": "Insurtech"})
        self.assertEqual(out["bands"], 1)
        row = self.profile.assessment_inputs.get(input_key="ANC_BOARD")
        self.assertEqual(row.value, "Good")
        self.assertEqual(row.unit, "Band")

    def test_an_unrequested_key_is_counted_rather_than_swallowed(self):
        out = self._run({"values": [
            {"inputKey": "NOT_A_KEY", "value": "9",
             "sourceUrl": "https://x.test", "confidence": 0.9}],
            "bands": [], "sector": "Fintech", "subSector": "Insurtech"})
        self.assertEqual(out["unknown_key_rejected"], 1)
        self.assertEqual(out["written"], out["benchmarks"])

    def test_an_unparseable_band_is_counted(self):
        out = self._run({"values": [], "bands": [
            {"inputKey": "ANC_BOARD", "band": "Pretty good",
             "sourceUrl": "https://x.test"}],
            "sector": "Fintech", "subSector": "Insurtech"})
        self.assertEqual(out["unparseable_band"], 1)


class DeterministicStageTwoTests(TestCase):
    """Stage 2 makes no model call at all."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if HAVE_WB:
            call_command("import_assessment_workbook", WORKBOOK,
                         config_version=1, activate=True, verbosity=0)

    def setUp(self):
        if not HAVE_WB:
            self.skipTest(skip_reason)
        from fundos.assessment.models import Assessment
        t, c = _company()
        self.a = Assessment.objects.create(company=c, tenant_id=t.id,
                                           deal_stage="Series A")

    def _pv(self, key, raw, **kw):
        from fundos.assessment.models import ParameterValue
        return ParameterValue.objects.create(
            assessment=self.a, input_key=key, raw_value=str(raw), **kw)

    def test_scoring_and_review_make_no_llm_call(self):
        """The guarantee, asserted rather than assumed."""
        from unittest.mock import patch

        from fundos.assessment.review import review
        from fundos.assessment.services import run_scoring

        self._pv("TEAM_FDR_EXP", "12", category="A")
        with patch("fundos.llm.adapter.llm_generate",
                   side_effect=AssertionError("stage 2 called a model")):
            run_scoring(self.a)
            review(self.a)

    def test_the_same_inputs_always_produce_the_same_score(self):
        """A model in the loop made this untrue between runs."""
        from fundos.assessment.services import run_scoring

        self._pv("TEAM_FDR_EXP", "12", category="A")
        self._pv("BQ_CO_AGE", "4", category="C")
        run_scoring(self.a)
        self.a.refresh_from_db()
        first = (self.a.overall_score, self.a.rating_band)
        for _ in range(3):
            run_scoring(self.a)
            self.a.refresh_from_db()
            self.assertEqual((self.a.overall_score, self.a.rating_band), first)

    def test_a_unit_error_is_flagged_rather_than_scored_silently(self):
        """4500 is 45% recorded in basis points. It bands Poor with complete
        confidence unless something says so."""
        from fundos.assessment.review import review

        self._pv("BQ_CHURN", "4500", unit="%", category="C")
        checks = [f["check"] for f in review(self.a)]
        self.assertIn("implausible_value", checks)

    def test_an_impossible_concentration_pair_is_caught(self):
        from fundos.assessment.review import review

        self._pv("BQ_TOP5_CLIENT", "20", unit="%", category="C")
        self._pv("BQ_TOP1_CLIENT", "75", unit="%", category="C")
        checks = [f["check"] for f in review(self.a)]
        self.assertIn("top_1_exceeds_top_5", checks)

    def test_a_category_resting_on_one_answer_is_flagged(self):
        from fundos.assessment.review import review
        from fundos.assessment.services import run_scoring

        self._pv("TEAM_FDR_EXP", "12", category="A")
        run_scoring(self.a)
        findings = [f for f in review(self.a) if f["check"] == "thin_category"]
        self.assertTrue(findings)

    def test_review_never_changes_a_score(self):
        """Findings describe. They do not re-band, which is precisely what
        the model-based review was permitted to do."""
        from fundos.assessment.review import review
        from fundos.assessment.services import run_scoring

        pv = self._pv("BQ_CHURN", "4500", unit="%", category="C")
        run_scoring(self.a)
        pv.refresh_from_db()
        before = (pv.band, pv.score)
        review(self.a)
        pv.refresh_from_db()
        self.assertEqual((pv.band, pv.score), before)

    def test_the_deprecated_entry_point_still_works(self):
        from fundos.assessment.tasks import run_rubric_review

        self._pv("BQ_CHURN", "4500", unit="%", category="C")
        findings = run_rubric_review(self.a)
        self.assertTrue(any(f["source"] == "rules" for f in findings))


# ===========================================================================
# v26 — why no search ever fired, and the call consolidation
# ===========================================================================

class SearchDirectiveTests(TestCase):
    """The model was only ever told a CEILING, never to search.

    On Gemini `google_search` is a MODEL-DECIDED tool: attaching it grants
    permission, it does not cause a search. The single sentence the model
    received about searching read "you may run AT MOST 6 searches… when the
    budget is spent, answer from what you have". Holding 24,000 characters of
    supplied context, it reasonably answered from context — on every call, in
    every run — while the tool was correctly attached, the response correctly
    parsed, and every layer reported healthy.
    """

    def test_a_search_expected_role_is_told_to_search_first(self):
        from fundos.llm.adapter import _search_directive

        text = _search_directive(6, expect_search=True)
        self.assertIn("SEARCH FIRST", text)
        self.assertIn("NOT OPTIONAL", text)
        self.assertIn("FAILED response", text)

    def test_other_roles_still_get_only_the_cap(self):
        """Roles answerable from supplied text must not be pushed to search —
        that would spend the budget for nothing."""
        from fundos.llm.adapter import _search_directive

        text = _search_directive(6)
        self.assertIn("AT MOST 6", text)
        self.assertNotIn("SEARCH FIRST", text)

    def test_the_roles_that_cannot_be_answered_locally_are_listed(self):
        from fundos.llm.adapter import SEARCH_EXPECTED_ROLES

        self.assertIn("assessment_inputs", SEARCH_EXPECTED_ROLES)
        self.assertIn("company_profile_deep_extract", SEARCH_EXPECTED_ROLES)

    def test_the_advanced_tier_really_does_carry_web_search(self):
        """Config was never the fault, and this pins that down so the next
        investigation starts somewhere new."""
        from fundos.llm.models import LLMConfigProfile
        from fundos.llm.tiers import resolve_tier_profile_code

        call_command("seed_platform_config", verbosity=0)
        code = resolve_tier_profile_code(role="assessment_inputs")
        caps = LLMConfigProfile.objects.get(code=code).capabilities_payload()
        self.assertIn("web_search", caps)
        # Gemini rejects google_search alongside a responseSchema, so the
        # seed turns the schema off on this tier. If that ever regresses the
        # call fails outright rather than silently skipping search.
        self.assertNotIn("structured_output", caps)


class CallConsolidationTests(TestCase):
    """Ten calls re-sent the same bundle to produce data already in hand.

    A run made 14 LLM calls. Eleven of them re-sent the same ~9,300-character
    source bundle: four `records`, three `structured`, three `section`,
    returning 56–283 completion tokens each against 2,791 prompt tokens
    apiece. Roughly 31,000 prompt tokens spent re-reading one 6KB website.
    """

    def test_the_deep_extract_returns_the_previously_refetched_blocks(self):
        from fundos.llm.default_prompts import get_default

        shape = get_default("company_profile_deep_extract")["system"]
        for block in ("products_and_services", "customers_and_markets",
                      "competitive_advantages", "metrics", "news"):
            self.assertIn(block, shape)

    # The three tests that stood here asserted properties of the consolidated
    # deep-extract fan-out: that a returned block was written before it was
    # "claimed", that an empty block was NOT claimed so the section still fell
    # through to its own call, and that the consolidated path was the default.
    #
    # All three described a mechanism the Company Master Data Pipeline
    # removes. There is no claim/fall-through any more, and no second path to
    # fall through TO: one synthesis call produces every section, writes the
    # ones it populated, and reports the rest as empty. Two of the three were
    # also source-text scans, which CHANGES_v29 §5.2 argues against directly —
    # they test the comment, not the behaviour, and both broke on a rename
    # rather than on a regression.
    #
    # What survives is the concern underneath them: a section the model did
    # not populate must be reported as empty, not quietly counted as done.
    # That is asserted behaviourally below, and again in
    # tests/api/test_profile_pipeline.py.

    def test_an_unpopulated_section_is_reported_empty_not_written(self):
        from django.core.management import call_command

        from fundos.core.scoping import tenant_context
        from fundos.profile import schema
        from fundos.profile.pipeline.writer import write_profile
        from fundos.profile.services import get_or_create_profile
        from tests.conftest_helpers import make_world

        # The section registry decides what is writable at all.
        call_command("seed_platform_config", verbosity=0)
        world = make_world()
        with tenant_context(world["a"]["tenant"].id):
            profile = get_or_create_profile(world["a"]["company"],
                                            user=world["a"]["founder"])
            generated = schema.empty_profile()
            # One section populated, the rest left as the model returned them.
            generated["sections"]["company_story"]["data"] = {
                "usp": "Fastest in category."}
            result = write_profile(profile, generated,
                                   user=world["a"]["founder"])

        self.assertEqual(result["written"], ["company_story"])
        self.assertIn("competitors", result["empty"],
                      "a section the model left empty must be REPORTED empty")
        self.assertEqual(result["failed"], [],
                         "an empty section is not a failure")
