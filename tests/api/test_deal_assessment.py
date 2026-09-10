"""Deal Assessment — config-driven scoring, domain agnostic."""
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase


class AssessmentConfigSeedTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)

    def test_category_weights_sum_to_one_hundred(self):
        """Integrity check #1 — every score depends on this."""
        from fundos.assessment.models import ConfigWeight
        total = sum((w.weight for w in ConfigWeight.objects.filter(
            level="category", is_active=True)), Decimal("0"))
        self.assertEqual(total, Decimal("100"))

    def test_every_stage_has_cut_points(self):
        """A rubric missing a stage silently un-scores that parameter for
        every company at that stage."""
        from fundos.assessment.models import ConfigRubric
        for key in ConfigRubric.objects.values_list(
                "input_key", flat=True).distinct():
            stages = set(ConfigRubric.objects.filter(
                input_key=key).values_list("stage", flat=True))
            self.assertEqual(
                stages, {"Seed", "Series A", "Series B", "Growth"},
                f"{key} is missing stage cut-points")

    def test_all_anchors_have_four_band_definitions(self):
        from fundos.assessment.models import ConfigAnchor
        self.assertEqual(ConfigAnchor.objects.count(), 24)
        for a in ConfigAnchor.objects.all():
            for f in ("excellent_def", "good_def", "fair_def", "poor_def"):
                self.assertTrue(getattr(a, f),
                                f"{a.input_key} missing {f}")

    def test_config_is_platform_level_not_per_tenant(self):
        """A new tenant must work with zero seeding."""
        from fundos.assessment.models import (ConfigAnchor, ConfigRubric,
                                              ConfigWeight)
        for m in (ConfigWeight, ConfigRubric, ConfigAnchor):
            self.assertFalse(
                m.objects.filter(tenant_id__isnull=False).exists(),
                f"{m.__name__} seeded per-tenant; it should be platform-wide")
            self.assertTrue(m.objects.filter(tenant_id__isnull=True).exists())

    def test_reference_rows_are_marked_and_excluded(self):
        from fundos.assessment.models import ConfigParameter
        self.assertTrue(
            ConfigParameter.objects.filter(feeds_score=False).exists())

    def test_seeding_is_idempotent(self):
        from fundos.assessment.models import ConfigRubric
        before = ConfigRubric.objects.count()
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)
        self.assertEqual(ConfigRubric.objects.count(), before)


class ScoringRunTests(TestCase):
    @classmethod
    def setUpTestData(cls):
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)

    def _assessment(self, stage="Series A"):
        from fundos.assessment.models import Assessment
        from fundos.core.models.identity import Company, Tenant
        t = Tenant.objects.create(name="T")
        c = Company.objects.create(tenant_id=t.id, name="Any Co")
        return Assessment.objects.create(company=c, tenant_id=t.id,
                                         deal_stage=stage)

    def test_stage_drives_banding(self):
        """Same value, different stage, different band — the whole point of
        stage-dependent rubrics."""
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import band_parameter

        a = self._assessment()
        pv = ParameterValue(assessment=a, input_key="TEAM_FDR_EXP",
                            raw_value="7")
        seed_band, _ = band_parameter(pv, "Seed")
        growth_band, _ = band_parameter(pv, "Growth")
        self.assertEqual(seed_band, "Good")       # Seed cuts [10,6,3]
        self.assertEqual(growth_band, "Fair")     # Growth cuts [15,10,5]

    def test_unset_stage_defaults_loudly(self):
        from fundos.assessment.services import resolve_stage
        a = self._assessment(stage="")
        with self.assertLogs("fundos.assessment.services", "WARNING"):
            stage, defaulted = resolve_stage(a)
        self.assertEqual(stage, "Series A")
        self.assertTrue(defaulted)

    def test_blank_input_is_excluded_not_zeroed(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import band_parameter
        a = self._assessment()
        pv = ParameterValue(assessment=a, input_key="TEAM_FDR_EXP",
                            raw_value="")
        band, score = band_parameter(pv, "Series A")
        self.assertIsNone(band)
        self.assertIsNone(score)

    def test_full_run_produces_score_rating_and_coverage(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring
        a = self._assessment()
        for key, val in (("TEAM_FDR_EXP", "12"), ("TEAM_COFDR_YRS", "7"),
                         ("TEAM_ADVISORS", "4"), ("TEAM_INST_INV", "3")):
            ParameterValue.objects.create(assessment=a, input_key=key,
                                          raw_value=val, category="A")
        out = run_scoring(a)
        a.refresh_from_db()
        self.assertIsNotNone(a.overall_score)
        self.assertIn(a.rating_band,
                      ["Excellent", "Very Good", "Good", "Average",
                       "Challenging", "Not Recommended"])
        self.assertLessEqual(float(a.overall_score), 9.0)   # cap by design
        self.assertTrue(out["rating_meaning"])
        self.assertGreater(a.category_scores.count(), 0)

    def test_uncommented_override_fails_the_audit(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring
        a = self._assessment()
        ParameterValue.objects.create(
            assessment=a, input_key="TEAM_FDR_EXP", raw_value="12",
            is_overridden=True, band="Exceptional", score=10,
            override_comment="")
        run_scoring(a)
        a.refresh_from_db()
        self.assertEqual(a.audit_status, "failed")
        self.assertTrue(any(f["check"] == "override_uncommented"
                            for f in a.audit_findings))

    def test_exceptional_without_override_is_flagged(self):
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_integrity_checks
        a = self._assessment()
        ParameterValue.objects.create(assessment=a, input_key="TEAM_FDR_EXP",
                                      band="Exceptional", is_overridden=False)
        findings = run_integrity_checks(a)
        self.assertTrue(any(f["check"] == "exceptional_without_override"
                            for f in findings))

    def test_override_survives_a_rescore(self):
        """A human decision must not be silently recomputed away."""
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring
        a = self._assessment()
        pv = ParameterValue.objects.create(
            assessment=a, input_key="TEAM_FDR_EXP", raw_value="1",
            is_overridden=True, band="Excellent", score=9,
            override_comment="Founder ran the category at a prior firm.")
        run_scoring(a)
        pv.refresh_from_db()
        self.assertEqual(pv.band, "Excellent")     # not recomputed to Poor
        self.assertEqual(pv.system_band, "Poor")   # system view retained

    def test_engine_is_domain_agnostic(self):
        """No sector/company assumptions anywhere in the config."""
        from fundos.assessment.models import ConfigRubric
        blob = " ".join(
            f"{r.input_key} {r.metric_name} {r.rationale}"
            for r in ConfigRubric.objects.all()).lower()
        for token in ("tyreplex", "zyla", "tyre"):
            self.assertNotIn(token, blob)
