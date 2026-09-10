"""v23 — Phase 3 (profile → assessment bridge) and Phase 4 (step 2 completion).

The two bugs these tests exist to keep fixed are both of the same kind: a
number that looked right and meant nothing.

  * Categories E and F carried 30% of every rating and had no parameters
    beneath them, so their weight silently redistributed on every assessment
    while the scorecard reported seven healthy categories.

  * Coverage was computed over the rows that existed rather than the model's
    parameter set, so it always read ~100% and the suppression gate that
    withholds a score built on thin evidence could never fire.

Neither raised an error. Both are asserted directly below.
"""
import os
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import temp_path


# The filled assessment workbook, when one is available. Not committed — it is
# a customer artefact — so the path is an environment variable rather than a
# hardcoded absolute path that resolves on exactly one machine.
#
# These tests must pass WITHOUT it. `seed_assessment_config` ships the same
# scoring model offline, and keeping the two in agreement is the point: if the
# fallback drifts from the sheet, an install with no workbook scores companies
# against a model nobody reviewed. That is what happened when the seeder wrote
# `SUB_TAM` for the workbook's `SEC_TAM_SUB`.
WORKBOOK = os.environ.get("FUNDOS_ASSESSMENT_WORKBOOK", "")


def _seed():
    """Config comes from the workbook when there is one, the seeder when not.

    Both paths must produce the same key contract — see the module docstring
    on `fundos.assessment.workbook`.
    """
    if WORKBOOK and os.path.exists(WORKBOOK):
        call_command("import_assessment_workbook", WORKBOOK,
                     config_version=1, activate=True, verbosity=0)
    else:
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)


def _company(name="Any Co"):
    from fundos.core.models import Company, Tenant
    t = Tenant.objects.create(name="T")
    return t, Company.objects.create(tenant_id=t.id, name=name)


def _profile(tenant, company, **kw):
    from fundos.profile.models import CompanyProfile
    return CompanyProfile.objects.create(
        tenant_id=tenant.id, company=company,
        website_url=kw.pop("website_url", "https://example.test"), **kw)


def _benchmarks():
    from fundos.assessment.models import SectorDealData, SectorMapping
    SectorDealData.objects.create(
        level="sector", name="Technology", deal_count=606,
        total_raised_usd_mn=Decimal("4977.55"),
        avg_ticket_usd_mn=Decimal("8.21"), investor_count=410,
        velocity_score=Decimal("10"), ticket_score=Decimal("2.5"),
        investors_score=Decimal("10"))
    SectorDealData.objects.create(
        level="sub_sector", name="SaaS", clubbed_group="SaaS",
        deal_count=120, velocity_score=Decimal("7"),
        ticket_score=Decimal("6"), investors_score=Decimal("8"))
    SectorMapping.objects.create(raw_label="B2B SaaS", clubbed_group="SaaS")
    SectorMapping.objects.create(raw_label="SaaS", clubbed_group="SaaS")


# ===========================================================================
# The config bug: 30% of the model could never score
# ===========================================================================

class SectorCategoryWiringTests(TestCase):
    """Categories E and F were unscoreable, and nothing said so."""

    @classmethod
    def setUpTestData(cls):
        _seed()

    def test_every_weighted_subitem_has_parameters(self):
        """The check that would have caught this at seed time.

        Weights summed to 100, every parameter had a rubric and every rubric
        had four stages — so every existing check passed while twelve
        sub-items carrying 30 weight-points had nothing beneath them.
        """
        from fundos.assessment.models import ConfigParameter, ConfigWeight

        subitems = {w.code for w in ConfigWeight.objects.filter(
            level="subitem", is_active=True)}
        parented = {p.parent_code or p.category_code
                    for p in ConfigParameter.objects.filter(is_active=True)}
        self.assertFalse(
            sorted(subitems - parented),
            "sub-items carry weight with no parameter beneath them; their "
            "share redistributes silently on every assessment")

    def test_category_f_has_parameters(self):
        from fundos.assessment.models import ConfigParameter
        self.assertGreater(
            ConfigParameter.objects.filter(is_active=True,
                                           category_code="F").count(), 0,
            "Sub-Sector carries 20% of the rating and had zero parameters")

    def test_no_parameter_rolls_up_to_a_missing_node(self):
        """'E.1 / F.1' split on '.' produced the parent 'E.1 / F'."""
        from fundos.assessment.models import ConfigParameter, ConfigWeight

        known = {w.code for w in ConfigWeight.objects.filter(
            is_active=True, level__in=("subitem", "category"))}
        orphans = [p.input_key for p in ConfigParameter.objects.filter(
            is_active=True)
            if (p.parent_code or p.category_code) not in known]
        self.assertEqual(orphans, [], "these are scored and roll up nowhere")

    def test_reference_rows_share_a_parent_with_what_they_cross_check(self):
        """'(ref)' suffixes orphaned the cross-checks into their own bucket.

        The contradiction test compares a ref row against its scored sibling
        in the same parent. While refs sat under 'C.6 (ref)' and the scored
        row under 'C.6', the two could never be compared and the check could
        never fire.
        """
        from fundos.assessment.models import ConfigParameter

        ref = ConfigParameter.objects.get(input_key="BQ_TOP1_CLIENT")
        scored = ConfigParameter.objects.get(input_key="BQ_TOP5_CLIENT")
        self.assertEqual(ref.parent_code, scored.parent_code)
        self.assertNotIn("(ref)", ref.ref_code)

    def test_shared_sector_metrics_are_seeded_for_both_categories(self):
        from fundos.assessment.models import ConfigParameter, ConfigRubric

        sub = ConfigParameter.objects.get(input_key="SEC_TAM_SUB")
        self.assertEqual(sub.category_code, "F")
        self.assertEqual(sub.parent_code, "F.1")
        # The mirror must carry the cut-points too, or it is a parameter that
        # can never band — the same failure in a different place.
        self.assertEqual(
            set(ConfigRubric.objects.filter(
                input_key="SEC_TAM_SUB").values_list("stage", flat=True)),
            {"Seed", "Series A", "Series B", "Growth"})

    def test_lookup_parameters_exist_for_the_benchmark_table(self):
        """Phase 2 loaded the data; nothing consumed it."""
        from fundos.assessment.models import ConfigParameter

        rows = ConfigParameter.objects.filter(is_active=True,
                                              scoring_type="lookup")
        self.assertEqual(
            {p.input_key for p in rows},
            {"PCTL_E_2", "PCTL_E_3", "PCTL_E_6",
             "PCTL_F_2", "PCTL_F_3", "PCTL_F_6"})
        # They are computed, not collected — so they must not inflate the
        # 80-row contract the sheet counts coverage against.
        self.assertFalse(any(p.is_workbook_input for p in rows))
        self.assertEqual({p.benchmark_metric for p in rows},
                         {"velocity", "ticket", "investors"})

    def test_percentile_is_banded_before_it_is_scored(self):
        """The percentile is NOT the score.

        v23 stored the percentile itself, so a ticket percentile of 10 scored
        10 and one of 2.5 scored 2.5. The workbook bands it first at 8/6/4
        and then scores 9/7/5/3 like every other row.
        """
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import band_parameter

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        for raw, band, score in (("10", "Excellent", 9.0),
                                 ("7.5", "Good", 7.0),
                                 ("4.2", "Fair", 5.0),
                                 ("2.5", "Poor", 3.0)):
            pv = ParameterValue(assessment=a, input_key="PCTL_E_2",
                                raw_value=raw)
            self.assertEqual(band_parameter(pv, "Series A"), (band, score),
                             f"percentile {raw}")

    def test_a_percentile_can_never_reach_exceptional(self):
        """10 is reachable only through a human override (§2.4.4)."""
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import band_parameter

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        pv = ParameterValue(assessment=a, input_key="PCTL_E_2",
                            raw_value="10")
        _band, score = band_parameter(pv, "Series A")
        self.assertLessEqual(score, 9.0)


# ===========================================================================
# Phase 3 — the bridge
# ===========================================================================

class ProfileBridgeTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed()

    def _assessment(self, stage="Series A"):
        from fundos.assessment.models import Assessment
        t, c = _company()
        return t, c, Assessment.objects.create(company=c, tenant_id=t.id,
                                               deal_stage=stage)

    def test_profile_values_reach_the_scorecard(self):
        """The gap this phase exists to close."""
        from fundos.assessment.profile_bridge import seed_from_profile
        from fundos.profile.models import ProfileAssessmentInput

        t, c, a = self._assessment()
        p = _profile(t, c)
        ProfileAssessmentInput.objects.create(
            profile=p, tenant_id=t.id, input_key="TEAM_FDR_EXP", value="12",
            unit="Years", source_type="profile", source_tier=2,
            confidence=Decimal("0.85"),
            source_url="https://example.test/team")

        out = seed_from_profile(a)
        self.assertEqual(out["seeded"], 1)
        pv = a.parameter_values.get(input_key="TEAM_FDR_EXP")
        self.assertEqual(pv.raw_value, "12")
        self.assertEqual(pv.source_type, "profile")
        self.assertIn("https://example.test/team", pv.source_detail)

    def test_extraction_writes_no_band_or_score_for_numeric_rows(self):
        """Facts have no opinions — the engine bands from published config."""
        from fundos.assessment.profile_bridge import seed_from_profile
        from fundos.profile.models import ProfileAssessmentInput

        t, c, a = self._assessment()
        p = _profile(t, c)
        ProfileAssessmentInput.objects.create(
            profile=p, tenant_id=t.id, input_key="TEAM_FDR_EXP", value="12",
            unit="Years", source_tier=2)
        seed_from_profile(a)
        pv = a.parameter_values.get(input_key="TEAM_FDR_EXP")
        self.assertEqual(pv.band, "")
        self.assertIsNone(pv.score)

    def test_anchor_rows_arrive_banded_and_are_revalidated(self):
        from fundos.assessment.profile_bridge import seed_from_profile
        from fundos.assessment.services import run_scoring
        from fundos.profile.models import ProfileAssessmentInput

        t, c, a = self._assessment()
        p = _profile(t, c)
        ProfileAssessmentInput.objects.create(
            profile=p, tenant_id=t.id, input_key="ANC_BOARD", value="Good",
            unit="Band", source_tier=3, confidence=Decimal("0.8"))
        seed_from_profile(a)
        run_scoring(a)
        pv = a.parameter_values.get(input_key="ANC_BOARD")
        self.assertEqual(pv.band, "Good")
        self.assertEqual(float(pv.score), 7.0)

    def test_unknown_keys_are_skipped_not_written(self):
        from fundos.assessment.profile_bridge import seed_from_profile
        from fundos.profile.models import ProfileAssessmentInput

        t, c, a = self._assessment()
        p = _profile(t, c)
        ProfileAssessmentInput.objects.create(
            profile=p, tenant_id=t.id, input_key="NOT_A_PARAMETER",
            value="9", source_tier=2)
        out = seed_from_profile(a)
        self.assertEqual(out["skipped_unknown"], 1)
        self.assertEqual(a.parameter_values.count(), 0)

    def test_missing_profile_is_not_an_error(self):
        """An assessment from documents alone is still a valid assessment."""
        from fundos.assessment.profile_bridge import seed_from_profile
        _t, _c, a = self._assessment()
        out = seed_from_profile(a)
        self.assertEqual(out["seeded"], 0)

    def test_blank_stays_blank_rather_than_zero(self):
        """§2.3.1 redistribution only works if absence is stored as absence."""
        from fundos.assessment.profile_bridge import seed_from_profile
        from fundos.profile.models import ProfileAssessmentInput

        t, c, a = self._assessment()
        p = _profile(t, c)
        ProfileAssessmentInput.objects.create(
            profile=p, tenant_id=t.id, input_key="BQ_PATENTS", value="",
            unit="Count", source_tier=2)
        out = seed_from_profile(a)
        self.assertEqual(out["skipped_blank"], 1)
        self.assertFalse(
            a.parameter_values.filter(input_key="BQ_PATENTS").exists(),
            "a researched blank must not become a scored zero")


class SourcePrecedenceTests(TestCase):
    """Tier decides, not recency and not confidence."""

    @classmethod
    def setUpTestData(cls):
        _seed()

    def _assessment(self):
        from fundos.assessment.models import Assessment
        t, c = _company()
        return Assessment.objects.create(company=c, tenant_id=t.id,
                                         deal_stage="Series A")

    def test_document_outranks_research(self):
        from fundos.assessment.profile_bridge import merge_value

        a = self._assessment()
        merge_value(a, "FIN_REV_SCALE", raw_value="40", source_type="profile",
                    source_tier=3, confidence=Decimal("0.95"))
        wrote = merge_value(a, "FIN_REV_SCALE", raw_value="52",
                            source_type="document", source_tier=1,
                            confidence=Decimal("0.7"))
        self.assertTrue(wrote)
        pv = a.parameter_values.get(input_key="FIN_REV_SCALE")
        self.assertEqual(pv.raw_value, "52")

    def test_high_confidence_does_not_beat_a_better_tier(self):
        """A model can be certain about a figure the audited model contradicts."""
        from fundos.assessment.profile_bridge import merge_value

        a = self._assessment()
        merge_value(a, "FIN_REV_SCALE", raw_value="52",
                    source_type="document", source_tier=1,
                    confidence=Decimal("0.6"))
        wrote = merge_value(a, "FIN_REV_SCALE", raw_value="40",
                            source_type="profile", source_tier=3,
                            confidence=Decimal("0.99"))
        self.assertFalse(wrote)
        self.assertEqual(
            a.parameter_values.get(input_key="FIN_REV_SCALE").raw_value, "52")

    def test_founder_confirmation_is_never_overwritten(self):
        from fundos.assessment.profile_bridge import merge_value

        a = self._assessment()
        merge_value(a, "BQ_CO_AGE", raw_value="7", source_type="founder",
                    source_tier=2)
        wrote = merge_value(a, "BQ_CO_AGE", raw_value="9",
                            source_type="document", source_tier=1,
                            confidence=Decimal("0.99"))
        self.assertFalse(wrote)
        self.assertEqual(
            a.parameter_values.get(input_key="BQ_CO_AGE").raw_value, "7")

    def test_confidence_breaks_a_tie_within_one_tier(self):
        from fundos.assessment.profile_bridge import merge_value

        a = self._assessment()
        merge_value(a, "BQ_CO_AGE", raw_value="7", source_type="profile",
                    source_tier=2, confidence=Decimal("0.5"))
        merge_value(a, "BQ_CO_AGE", raw_value="9", source_type="profile",
                    source_tier=2, confidence=Decimal("0.9"))
        self.assertEqual(
            a.parameter_values.get(input_key="BQ_CO_AGE").raw_value, "9")

    def test_founder_confirmed_inputs_survive_a_regeneration(self):
        from fundos.profile.assessment_extraction import _persist
        from fundos.profile.models import ProfileAssessmentInput

        t, c = _company()
        p = _profile(t, c)
        ProfileAssessmentInput.objects.create(
            profile=p, tenant_id=t.id, input_key="TEAM_FDR_EXP", value="15",
            unit="Years", is_founder_confirmed=True)
        _persist(p, [{"input_key": "TEAM_FDR_EXP", "value": "9",
                      "unit": "Years", "source_type": "profile",
                      "source_url": "", "source_tier": 3, "confidence": 0.9,
                      "justification": ""}])
        self.assertEqual(
            ProfileAssessmentInput.objects.get(
                profile=p, input_key="TEAM_FDR_EXP").value, "15")


class SubSectorResolutionTests(TestCase):
    """Category F is 20% of the rating; the label decides the population."""

    @classmethod
    def setUpTestData(cls):
        _seed()
        _benchmarks()

    def test_exact_raw_label_resolves_to_its_group(self):
        from fundos.profile.assessment_extraction import resolve_sub_sector
        group, method = resolve_sub_sector("B2B SaaS")
        self.assertEqual(group, "SaaS")
        self.assertEqual(method, "exact")

    def test_unknown_label_is_left_unresolved_rather_than_guessed(self):
        """Wrong is worse than blank: blank redistributes, wrong scores."""
        from fundos.profile.assessment_extraction import resolve_sub_sector
        group, method = resolve_sub_sector("Artisanal Cheese Logistics")
        self.assertIsNone(group)
        self.assertEqual(method, "unresolved")

    def test_benchmark_rows_are_emitted_for_both_levels(self):
        from fundos.profile.assessment_extraction import benchmark_inputs
        rows, group, _method = benchmark_inputs("Technology", "B2B SaaS")
        self.assertEqual(group, "SaaS")
        keys = {r["input_key"] for r in rows}
        self.assertEqual(keys, {"PCTL_E_2", "PCTL_E_3", "PCTL_E_6",
                                "PCTL_F_2", "PCTL_F_3", "PCTL_F_6"})

    def test_benchmark_values_are_the_workbook_scores(self):
        from fundos.profile.assessment_extraction import benchmark_inputs
        rows, _g, _m = benchmark_inputs("Technology", "SaaS")
        by_key = {r["input_key"]: r["value"] for r in rows}
        self.assertEqual(Decimal(by_key["PCTL_E_3"]), Decimal("2.50"))
        self.assertEqual(Decimal(by_key["PCTL_F_2"]), Decimal("7.00"))

    def test_sectors_e_and_f_actually_score(self):
        """The end of the 30%-unscoreable bug, asserted on a real run."""
        from fundos.assessment.models import Assessment
        from fundos.assessment.profile_bridge import seed_from_profile
        from fundos.assessment.services import run_scoring
        from fundos.profile.assessment_extraction import benchmark_inputs
        from fundos.profile.models import ProfileAssessmentInput

        t, c = _company()
        p = _profile(t, c)
        rows, _g, _m = benchmark_inputs("Technology", "SaaS")
        for r in rows:
            ProfileAssessmentInput.objects.create(
                profile=p, tenant_id=t.id, input_key=r["input_key"],
                value=r["value"], unit=r["unit"],
                source_type=r["source_type"], source_tier=r["source_tier"])

        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        seed_from_profile(a)
        run_scoring(a)

        scores = {cs.category_code: cs.score for cs in a.category_scores.all()}
        self.assertIsNotNone(scores.get("E"), "Sector still not scoring")
        self.assertIsNotNone(scores.get("F"), "Sub-Sector still not scoring")


# ===========================================================================
# Phase 4 — stage gating, coverage shape, review log
# ===========================================================================

class CoverageTests(TestCase):
    """Coverage was its own denominator, so it always read ~100%."""

    @classmethod
    def setUpTestData(cls):
        _seed()

    def _assessment(self):
        from fundos.assessment.models import Assessment
        t, c = _company()
        return Assessment.objects.create(company=c, tenant_id=t.id,
                                         deal_stage="Series A")

    def test_coverage_is_measured_against_the_whole_model(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        a = self._assessment()
        for key, val in (("TEAM_FDR_EXP", "12"), ("TEAM_COFDR_YRS", "7")):
            ParameterValue.objects.create(assessment=a, input_key=key,
                                          raw_value=val, category="A")
        run_scoring(a)
        a.refresh_from_db()
        self.assertLess(
            float(a.input_coverage_pct), 20,
            "two answered parameters out of eighty is not high coverage")

    def test_thin_evidence_still_trips_the_suppression_gate(self):
        """The gate could never fire while the denominator was the numerator."""
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring
        from fundos.assessment.views import COVERAGE_FLOOR_PCT

        a = self._assessment()
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="12", category="A")
        run_scoring(a)
        a.refresh_from_db()
        self.assertLess(float(a.input_coverage_pct), COVERAGE_FLOOR_PCT)

    def test_low_coverage_raises_an_audit_finding(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        a = self._assessment()
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="12", category="A")
        run_scoring(a)
        a.refresh_from_db()
        self.assertTrue(any(f["check"] == "low_coverage"
                            for f in a.audit_findings))

    def test_coverage_breakdown_names_what_is_missing_per_category(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import coverage_by_category, run_scoring

        a = self._assessment()
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="12", category="A")
        run_scoring(a)
        rows = {r["categoryCode"]: r for r in coverage_by_category(a)}
        self.assertGreater(rows["A"]["answered"], 0)
        self.assertEqual(rows["B"]["answered"], 0)
        self.assertTrue(rows["B"]["missing"],
                        "an empty category must name what it wants")


class StageGateTests(TestCase):

    @classmethod
    def setUpTestData(cls):
        _seed()

    def test_explicit_stage_beats_the_deal_default(self):
        from fundos.assessment.tasks import resolve_initial_stage

        class Deal:
            stage = "Seed"
        self.assertEqual(resolve_initial_stage(Deal(), "Growth"), "Growth")

    def test_deal_stage_is_used_when_none_is_requested(self):
        from fundos.assessment.tasks import resolve_initial_stage

        class Deal:
            stage = "series b"
        self.assertEqual(resolve_initial_stage(Deal()), "Series B")

    def test_an_invalid_request_does_not_silently_become_a_stage(self):
        from fundos.assessment.tasks import resolve_initial_stage

        class Deal:
            stage = None
        self.assertEqual(resolve_initial_stage(Deal(), "Pre-Seed"), "")

    def test_changing_stage_rebands_the_scorecard(self):
        """The reason stage is gated: same value, different band."""
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import run_scoring

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Seed")
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="7", category="A")
        run_scoring(a)
        self.assertEqual(
            a.parameter_values.get(input_key="TEAM_FDR_EXP").band, "Good")

        a.deal_stage = "Growth"
        a.save(update_fields=["deal_stage"])
        run_scoring(a)
        self.assertEqual(
            a.parameter_values.get(input_key="TEAM_FDR_EXP").band, "Fair")

    def test_defaulted_stage_is_flagged_in_the_audit(self):
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import run_scoring

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="")
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="7", category="A")
        run_scoring(a)
        a.refresh_from_db()
        self.assertTrue(a.stage_was_defaulted)
        self.assertTrue(any(f["check"] == "stage_defaulted"
                            for f in a.audit_findings))


class ReviewLogTests(TestCase):
    """The table shipped in migration 0001 and nothing ever wrote to it."""

    @classmethod
    def setUpTestData(cls):
        _seed()

    def test_an_override_writes_a_review_trail_entry(self):
        from fundos.assessment.models import (Assessment, ParameterValue,
                                              ReviewLog)

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        pv = ParameterValue.objects.create(
            assessment=a, input_key="TEAM_FDR_EXP", raw_value="7",
            category="A", band="Good", score=Decimal("7"))

        # Mirrors what ParameterOverrideView writes.
        ReviewLog.objects.create(
            assessment=a, field_or_topic="TEAM_FDR_EXP",
            previous_value=f"{pv.raw_value} [{pv.band}]",
            founder_value="9 [Excellent]",
            founder_reason="Two prior ventures in the same category.",
            agent_action="Override applied.", status="closed")

        row = ReviewLog.objects.get(assessment=a)
        self.assertEqual(row.field_or_topic, "TEAM_FDR_EXP")
        self.assertIn("Good", row.previous_value)
        self.assertEqual(row.status, "closed")

    def test_a_rejected_challenge_is_kept_not_deleted(self):
        """The pattern of what gets rejected is how a bad cut-point is found."""
        from fundos.assessment.models import Assessment, ReviewLog

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        row = ReviewLog.objects.create(
            assessment=a, field_or_topic="FIN_RUNWAY",
            founder_reason="Runway assumes a burn we have already cut.",
            status="open")
        row.status = "rejected"
        row.agent_action = "Cut is not yet reflected in the filed accounts."
        row.save()

        self.assertEqual(ReviewLog.objects.filter(assessment=a).count(), 1)
        self.assertEqual(ReviewLog.objects.get(id=row.id).status, "rejected")


class ContradictionCheckTests(TestCase):
    """Now reachable: refs finally share a parent with their scored sibling."""

    @classmethod
    def setUpTestData(cls):
        _seed()

    def test_a_ref_contradicting_its_sibling_is_surfaced(self):
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import run_scoring

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        # Top-5 concentration looks healthy; top-1 alone is most of revenue.
        ParameterValue.objects.create(assessment=a, input_key="BQ_TOP5_CLIENT",
                                      raw_value="20", category="C")
        ParameterValue.objects.create(assessment=a, input_key="BQ_TOP1_CLIENT",
                                      raw_value="75", category="C")
        run_scoring(a)
        a.refresh_from_db()
        self.assertTrue(
            any(f["check"] == "ref_contradiction" for f in a.audit_findings),
            "the cross-check exists precisely to catch this")


class AssessmentInputExtractionTests(TestCase):
    """The step 1 side, exercised through the mocked role."""

    @classmethod
    def setUpTestData(cls):
        _seed()
        _benchmarks()

    def test_extraction_writes_typed_rows_with_provenance(self):
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)
        from fundos.profile.models import ProfileAssessmentInput

        t, c = _company()
        p = _profile(t, c)
        out = extract_assessment_inputs(
            p, payloads={"website": {"text": "x" * 900},
                     "documents": [{"text": "deck"}]})
        self.assertGreater(out["written"], 0)
        for row in ProfileAssessmentInput.objects.filter(profile=p):
            self.assertTrue(row.input_key)
            self.assertIsNotNone(row.source_tier)

    def test_extraction_is_partial_by_design(self):
        """A real run never answers everything; the mock must not either."""
        from fundos.profile.assessment_extraction import (
            extract_assessment_inputs)

        t, c = _company()
        p = _profile(t, c)
        out = extract_assessment_inputs(
            p, payloads={"website": {"text": "x" * 900},
                     "documents": [{"text": "deck"}]})
        self.assertGreater(out["written"], 0)
        self.assertLess(out["written"], out["asked"])

    def test_financial_parameters_are_not_researched(self):
        """Category B comes from the uploaded model, never from a website."""
        from fundos.profile.assessment_extraction import obtainable_keys

        for key in ("FIN_REV_SCALE", "FIN_CM1", "FIN_EBITDA_M",
                    "FIN_RUNWAY"):
            self.assertNotIn(key, obtainable_keys(),
                             f"{key} must come from the financial model")

    def test_mandate_parameters_are_not_researched(self):
        """Category G is internal to the advisor; the company cannot know it."""
        from fundos.profile.assessment_extraction import obtainable_keys

        for key in ("IB_SECTOR_DEALS", "IB_LIVE_MANDATES", "IB_DEAL_SIZE"):
            self.assertNotIn(key, obtainable_keys())

    def test_the_role_is_declared_and_bindable(self):
        """An undeclared role cannot be bound in admin and fails at runtime."""
        from fundos.llm.default_prompts import get_default, get_tier
        from fundos.llm.models import LLM_ROLES

        self.assertIn("assessment_inputs", LLM_ROLES)
        shipped = get_default("assessment_inputs")
        self.assertTrue(shipped and shipped.get("system"),
                        "a role with no shipped prompt sends an empty system")
        # ADVANCED: roughly half these parameters — sector TAM, peer age, peer
        # raise recency — are not in the company's own material at all.
        self.assertEqual(get_tier("assessment_inputs"), "advanced")

    def test_the_role_schema_forbids_asserting_a_score(self):
        from fundos.llm import schemas

        out = schemas.validate("assessment_inputs",
                               {"values": [], "score": 9.9})
        self.assertNotIn("score", out)


# ===========================================================================
# The Phase 4 endpoints, over the real HTTP stack
# ===========================================================================

class PhaseFourEndpointTests(TestCase):
    """Routing, auth and payload shape — a view nothing routes is not shipped."""

    @classmethod
    def setUpTestData(cls):
        _seed()

    def setUp(self):
        from tests.conftest_helpers import auth_headers, make_world
        self.world = make_world()
        self.deal = self.world["a"]["deal"]
        self.user = self.world["a"]["founder"]
        self.hdrs = auth_headers(self.user)
        self.base = f"/api/v1/deals/{self.deal.id}/assessment"

    def _assessment(self, stage="Series A"):
        from fundos.assessment.models import Assessment, ParameterValue
        a = Assessment.objects.create(
            company=self.world["a"]["company"],
            tenant_id=self.world["a"]["tenant"].id, deal=self.deal,
            deal_stage=stage, status="scored")
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      raw_value="7", category="A")
        return a

    # --- stage ----------------------------------------------------------
    def test_stage_options_are_offered(self):
        self._assessment()
        r = self.client.get(f"{self.base}/stage", **self.hdrs)
        self.assertEqual(r.status_code, 200)
        self.assertIn("Series A",
                      [o["value"] for o in r.json()["options"]])

    def test_setting_stage_rescores_and_reports_the_move(self):
        self._assessment(stage="Seed")
        r = self.client.post(f"{self.base}/stage",
                             {"dealStage": "Growth"},
                             content_type="application/json", **self.hdrs)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertEqual(body["dealStage"], "Growth")
        self.assertEqual(body["previousStage"], "Seed")

    def test_an_invalid_stage_is_rejected(self):
        self._assessment()
        r = self.client.post(f"{self.base}/stage", {"dealStage": "Pre-Seed"},
                             content_type="application/json", **self.hdrs)
        self.assertEqual(r.status_code, 400)

    def test_confirming_the_stage_clears_the_defaulted_flag(self):
        from fundos.assessment.services import run_scoring

        a = self._assessment(stage="")
        run_scoring(a)
        a.refresh_from_db()
        self.assertTrue(a.stage_was_defaulted)

        self.client.post(f"{self.base}/stage", {"dealStage": "Series A"},
                         content_type="application/json", **self.hdrs)
        a.refresh_from_db()
        self.assertFalse(a.stage_was_defaulted)

    # --- coverage -------------------------------------------------------
    def test_coverage_endpoint_ranks_gaps_by_rating_weight(self):
        """A blank in Sub-Sector costs twice what one in Business Quality does."""
        from fundos.assessment.services import run_scoring

        a = self._assessment()
        run_scoring(a)
        r = self.client.get(f"{self.base}/coverage", **self.hdrs)
        self.assertEqual(r.status_code, 200)
        body = r.json()
        self.assertTrue(body["scoreSuppressed"])
        priority = body["byPriority"]
        self.assertGreaterEqual(priority[0]["unevidencedWeight"],
                                priority[-1]["unevidencedWeight"])

    # --- review log -----------------------------------------------------
    def test_a_challenge_can_be_raised_and_resolved(self):
        self._assessment()
        r = self.client.post(
            f"{self.base}/review-log",
            {"fieldOrTopic": "TEAM_FDR_EXP",
             "founderValue": "11",
             "founderReason": "Counts only the incorporated years."},
            content_type="application/json", **self.hdrs)
        self.assertEqual(r.status_code, 201)
        entry_id = r.json()["id"]
        self.assertIn("7", r.json()["previousValue"])

        listed = self.client.get(f"{self.base}/review-log", **self.hdrs)
        self.assertEqual(listed.json()["openCount"], 1)

        closed = self.client.patch(
            f"{self.base}/review-log/{entry_id}",
            {"status": "closed", "agentAction": "Corrected from the filing."},
            content_type="application/json", **self.hdrs)
        self.assertEqual(closed.status_code, 200)
        self.assertEqual(closed.json()["status"], "closed")

    def test_a_challenge_without_a_reason_is_rejected(self):
        self._assessment()
        r = self.client.post(f"{self.base}/review-log",
                             {"fieldOrTopic": "TEAM_FDR_EXP"},
                             content_type="application/json", **self.hdrs)
        self.assertEqual(r.status_code, 400)

    def test_closing_a_challenge_requires_saying_what_was_done(self):
        """Rejecting silently is the failure this table exists to prevent."""
        from fundos.assessment.models import ReviewLog

        a = self._assessment()
        row = ReviewLog.objects.create(assessment=a,
                                       field_or_topic="FIN_RUNWAY",
                                       founder_reason="Burn already cut.",
                                       status="open")
        r = self.client.patch(f"{self.base}/review-log/{row.id}",
                              {"status": "rejected"},
                              content_type="application/json", **self.hdrs)
        self.assertEqual(r.status_code, 400)

    # --- scorecard additions --------------------------------------------
    def test_scorecard_reports_stage_confirmation_and_evidence_mix(self):
        from fundos.assessment.services import run_scoring

        a = self._assessment()
        a.parameter_values.update(source_type="profile")
        run_scoring(a)
        r = self.client.get(self.base, **self.hdrs)
        body = r.json()
        self.assertIn("stageConfirmed", body)
        self.assertIn("evidenceMix", body)
        self.assertIn("profile",
                      [m["sourceType"] for m in body["evidenceMix"]])

    def test_an_override_is_visible_in_the_review_log(self):
        """Phase 4: the override endpoint now writes the trail."""
        from fundos.assessment.services import run_scoring

        a = self._assessment()
        run_scoring(a)
        r = self.client.post(
            f"{self.base}/parameters/TEAM_FDR_EXP/override",
            {"score": 9, "reason": "Two prior exits in this category."},
            content_type="application/json", **self.hdrs)
        self.assertEqual(r.status_code, 200)

        listed = self.client.get(f"{self.base}/review-log", **self.hdrs)
        topics = [i["fieldOrTopic"] for i in listed.json()["items"]]
        self.assertIn("TEAM_FDR_EXP", topics)


class DiagnosticLoggingTests(TestCase):
    """The assessment half of the pipeline must reach the diagnostic file.

    `silk_generation.log` is the artefact sent when asking why a deal scored
    the way it did. Until v23 `fundos.assessment` was not routed to it, so the
    file carried the whole profile pipeline and nothing at all about the
    bridge, extraction or scoring — misleading rather than merely incomplete.
    """

    def test_assessment_logger_writes_to_the_generation_file(self):
        from django.conf import settings

        cfg = settings.LOGGING["loggers"].get("fundos.assessment")
        self.assertIsNotNone(
            cfg, "fundos.assessment is not routed; its logs reach console only")
        self.assertIn("generation_file", cfg["handlers"])

    def test_the_bridge_logs_its_counts(self):
        """The counts are the first thing read when coverage looks wrong."""
        from fundos.assessment.models import Assessment
        from fundos.assessment.profile_bridge import seed_from_profile

        _seed()
        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        with self.assertLogs("fundos.assessment.profile_bridge", "INFO") as log:
            seed_from_profile(a)
        self.assertTrue(any("BRIDGE" in line for line in log.output))


class WorkbookFormatTests(TestCase):
    """The format the admin upload actually requires.

    Both cases below failed silently before v23 — no error, no skipped-row
    entry, just a lower number than expected in a report nobody reads closely.
    """

    def _sheet(self, path, sub_header="Clubbed Sub-Sector",
               sector_cols=None):
        from openpyxl import Workbook

        cols = sector_cols or ["Sector", "# Deals", "Raised $Mn",
                               "Avg Ticket $Mn", "# Investors"]
        wb = Workbook()
        ws = wb.active
        ws.title = "Sector Deal Data"
        for i, h in enumerate(cols, 1):
            ws.cell(row=1, column=i, value=h)
        # Block 2 sits to the right and runs PAST block 1.
        ws.cell(row=1, column=8, value="Raw Sub-Sector")
        ws.cell(row=1, column=9, value="Clubbed Group")
        for r, (n, d) in enumerate([("Ecommerce", 606), ("Fintech", 397)], 2):
            ws.cell(row=r, column=1, value=n)
            ws.cell(row=r, column=2, value=d)
            ws.cell(row=r, column=3, value=100.0)
            ws.cell(row=r, column=5, value=50)
        ws.cell(row=4, column=1, value="Total — Sector Table")
        ws.cell(row=4, column=2, value=1003)
        for r, (raw, grp) in enumerate(
                [("B2B SaaS", "SaaS"), ("Vertical SaaS", "SaaS"),
                 ("Payments", "Payments"), ("Neobank", "Payments")], 2):
            ws.cell(row=r, column=8, value=raw)
            ws.cell(row=r, column=9, value=grp)

        ws.cell(row=7, column=1, value=sub_header)
        for i, h in enumerate(["# Deals", "Raised $Mn", "Avg Ticket $Mn",
                               "# Investors"], 2):
            ws.cell(row=7, column=i, value=h)
        for r, (n, d) in enumerate([("SaaS", 120), ("Payments", 151)], 8):
            ws.cell(row=r, column=1, value=n)
            ws.cell(row=r, column=2, value=d)
            ws.cell(row=r, column=3, value=80.0)
            ws.cell(row=r, column=5, value=60)
        # The sheet's own integrity check, below the tables.
        ws.cell(row=11, column=1, value="Deals in source extract (input)")
        ws.cell(row=11, column=2, value=2699)
        wb.save(path)
        return path

    def test_natural_sub_sector_header_is_accepted(self):
        """'Clubbed Sub-Sector' detected no name column, so block 3 read zero
        rows — and category F then scored against an empty table while every
        other number in the import report looked correct."""
        from fundos.assessment.benchmarks import parse_workbook

        with temp_path(".xlsx") as path:
            sectors, subs, report = parse_workbook(self._sheet(path))
        self.assertEqual(len(sectors), 2)
        self.assertEqual([r["name"] for r in subs], ["SaaS", "Payments"])

    def test_alternate_sub_sector_headers_also_work(self):
        from fundos.assessment.benchmarks import parse_workbook

        for header in ("Sub-Sector", "Clubbed Group", "Name", "Sector"):
            with temp_path(".xlsx") as path:
                _s, subs, _r = parse_workbook(
                    self._sheet(path, sub_header=header))
            self.assertEqual(len(subs), 2, f"header {header!r} read no rows")

    def test_score_columns_must_follow_their_count_columns(self):
        """'Investors Score' starts with 'investors', so placed BEFORE
        '# Investors' it is captured as the investor COUNT and the real count
        is never read. Order is part of the format, not a preference."""
        from fundos.assessment.benchmarks import SECTOR_COLUMNS, detect_columns

        wrong = detect_columns(
            ["Sector", "# Deals", "Investors Score", "# Investors"],
            SECTOR_COLUMNS)
        self.assertNotIn("investors_score", wrong)

        right = detect_columns(
            ["Sector", "# Deals", "# Investors", "Investors Score"],
            SECTOR_COLUMNS)
        self.assertIn("investors_score", right)
        self.assertIn("investor_count", right)

    def test_totals_and_integrity_rows_never_import(self):
        from fundos.assessment.benchmarks import parse_workbook

        with temp_path(".xlsx") as path:
            sectors, subs, _r = parse_workbook(self._sheet(path))
        names = [r["name"] for r in sectors + subs]
        self.assertNotIn("Total — Sector Table", names)
        self.assertFalse([n for n in names if n.startswith("Deals in source")])

    def test_the_raw_mapping_table_is_read_past_the_sector_block(self):
        """Block 2 is taller than block 1; bounding it to the block dropped
        most of the mappings."""
        from fundos.assessment.benchmarks import parse_workbook

        with temp_path(".xlsx") as path:
            _s, _su, report = parse_workbook(self._sheet(path))
        self.assertEqual(report["raw_sub_sector_mappings"], 4)

    def test_a_missing_deal_count_column_fails_loudly(self):
        """Header detection keys on a deal-count column. With none anywhere,
        the importer must raise rather than import an empty table."""
        from openpyxl import Workbook
        from fundos.assessment.benchmarks import parse_workbook

        with temp_path(".xlsx") as path:
            wb = Workbook()
            ws = wb.active
            ws.title = "Sector Deal Data"
            for i, h in enumerate(["Sector", "Count", "Raised $Mn"], 1):
                ws.cell(row=1, column=i, value=h)
            ws.cell(row=2, column=1, value="Ecommerce")
            ws.cell(row=2, column=2, value=606)
            wb.save(path)
            with self.assertRaises(ValueError):
                parse_workbook(path)

    def test_a_missing_sheet_names_the_sheets_present(self):
        from openpyxl import Workbook
        from fundos.assessment.benchmarks import parse_workbook

        with temp_path(".xlsx") as path:
            wb = Workbook()
            wb.active.title = "Something Else"
            wb.save(path)
            with self.assertRaises(ValueError) as ctx:
                parse_workbook(path)
        self.assertIn("Something Else", str(ctx.exception))


class ParameterPopulationCoverageTests(TestCase):
    """Every scored parameter must have SOMETHING that can answer it.

    A parameter no source populates is invisible: it has a rubric, it passes
    every integrity check, and it scores blank on every company forever while
    its weight redistributes onto its siblings. That is the same class of
    defect as the stranded category-F weight, one level further down.
    """

    @classmethod
    def setUpTestData(cls):
        _seed()

    def test_every_scored_parameter_has_a_declared_source_of_record(self):
        """A parameter nobody owns scores blank on every company forever."""
        from fundos.assessment.models import ConfigParameter
        from fundos.assessment.parameter_sources import unsourced

        self.assertEqual(
            unsourced(ConfigParameter.objects.filter(is_active=True)), [],
            "these rows have no source responsible for answering them")

    def test_research_covers_everything_it_is_declared_to_cover(self):
        """RESEARCH is a promise: if source_for() says step 1 answers a
        parameter, step 1 must actually ask for it."""
        from fundos.assessment.models import ConfigParameter
        from fundos.assessment.parameter_sources import RESEARCH, source_for
        from fundos.profile.assessment_extraction import obtainable_keys

        asked = obtainable_keys()
        missing = [p.input_key
                   for p in ConfigParameter.objects.filter(
                       is_active=True, feeds_score=True)
                   if source_for(p) == RESEARCH and p.input_key not in asked]
        self.assertEqual(missing, [])

    def test_missing_evidence_names_the_document_not_a_placeholder(self):
        """The block exists to turn a coverage gap into a task."""
        from fundos.assessment.models import Assessment
        from fundos.assessment.views import AssessmentView

        t, c = _company()
        a = Assessment.objects.create(company=c, tenant_id=t.id,
                                      deal_stage="Series A")
        rows = AssessmentView._missing_evidence(a)
        labels = {r["document"] for r in rows}
        self.assertTrue(rows)
        self.assertNotIn("Additional information required", labels)
        self.assertIn("Financial model (uploaded)", labels)

    def test_both_sector_and_sub_sector_are_asked_separately(self):
        """F is worth double E, and the workbook spells the sub-sector keys
        with a `_SUB` SUFFIX — its own band formula does
        SUBSTITUTE(key,"_SUB","") to find the rubric. v23 wrote `SUB_TAM`,
        which matched nothing at all."""
        from fundos.profile.assessment_extraction import obtainable_keys

        asked = obtainable_keys()
        for key in ("SEC_TAM", "SEC_TAM_SUB", "SEC_TAM_CAGR",
                    "SEC_TAM_CAGR_SUB", "SEC_PEER_AGE", "SEC_PEER_AGE_SUB",
                    "SEC_PEER_RAISE_MTHS", "SEC_PEER_RAISE_MTHS_SUB"):
            self.assertIn(key, asked)
        for wrong in ("SUB_TAM", "SUB_PEER_AGE"):
            self.assertNotIn(wrong, asked, "not a workbook key")
