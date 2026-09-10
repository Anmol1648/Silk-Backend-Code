"""Stage 2 Phase 1 — the Deal Scorecard screen's two endpoints.

Every assertion runs against the REAL seeded scoring model: the seven category
weights, the 52 stage rubrics and the 24 anchors the platform ships, not a
fixture cut down to whatever the test needed. That is the point of the seeder
being platform-wide — a test that stubbed the config would prove the reshaping
works and prove nothing about whether it reshapes the model this product
actually scores against.
"""
from decimal import Decimal

from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import auth_headers, make_world


class Phase1Base(TestCase):
    """A scored company with evidence spread across four categories."""

    @classmethod
    def setUpTestData(cls):
        call_command("seed_assessment_config", config_version=1,
                     activate=True, verbosity=0)

    def setUp(self):
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import run_scoring

        self.world = make_world()
        self.founder = self.world["a"]["founder"]
        self.company = self.world["a"]["company"]
        self.deal = self.world["a"]["deal"]
        self.headers = auth_headers(self.founder)

        self.assessment = Assessment.objects.create(
            company=self.company, deal=self.deal,
            tenant_id=self.world["a"]["tenant"].id,
            deal_stage="Series A", sector="Ecommerce",
            sub_sector="B2B Ecommerce", capital_raised_usd_mn=Decimal("5"))

        # Values chosen to land on known bands at Series A cut-points, so the
        # assertions below are about the projection rather than about luck.
        rows = [
            # key,                 value, tier, confidence, notes
            ("TEAM_FDR_EXP", "12", 1, "0.95", ""),        # >=10 -> Excellent
            ("TEAM_COFDR_YRS", "5", 1, "0.90", ""),       # [6,4,2] -> Good
            ("TEAM_INST_INV", "2", 2, "0.72", ""),        # [3,2,1] -> Good
            ("TEAM_ADVISORS", "1", 3, "0.30",
             "Ask the founder for the advisory board roster."),
        ]
        for key, value, tier, conf, notes in rows:
            ParameterValue.objects.create(
                assessment=self.assessment, input_key=key, raw_value=value,
                category="A", source_type="document", source_tier=tier,
                source_detail=f"deck, slide 3 ({key})",
                confidence=Decimal(conf), notes=notes,
                justification=f"Read directly for {key}.")
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _get(self):
        return self.client.get(
            f"/api/v1/companies/{self.company.id}/fundraising/phase1",
            **self.headers)

    def _override(self, **body):
        return self.client.post(
            f"/api/v1/companies/{self.company.id}/fundraising/phase1/override",
            body, content_type="application/json", **self.headers)


CONTRACT_KEYS = [
    "company", "sector", "sub_sector", "deal_stage", "ask_amount",
    "capital_raised", "assessment_date", "overall_score", "deal_rating",
    "input_coverage", "strongest_category", "weakest_category",
    "executiveSummaryData", "dealScorecardData", "diligenceFindingsData",
    "bandRecommendationsData",
]


class Phase1ContractTests(Phase1Base):

    def test_every_contract_key_is_present_at_the_top_level(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        for key in CONTRACT_KEYS:
            self.assertIn(key, r.data, f"{key} missing from the contract")

    def test_summary_strip_is_the_real_company_and_assessment(self):
        r = self._get().data
        self.assertEqual(r["company"], self.company.name)
        self.assertEqual(r["sector"], "Ecommerce")
        self.assertEqual(r["sub_sector"], "B2B Ecommerce")
        self.assertEqual(r["deal_stage"], "Series A")
        self.assertEqual(r["assessment_date"], self.assessment.assessment_date)
        self.assertEqual(Decimal(str(r["overall_score"])),
                         self.assessment.overall_score)
        self.assertEqual(r["deal_rating"], self.assessment.rating_band)
        self.assertEqual(Decimal(str(r["input_coverage"])),
                         self.assessment.input_coverage_pct)

    def test_strongest_and_weakest_come_from_the_scored_rollups(self):
        r = self._get().data
        cats = {c["ref"]: c for c in r["dealScorecardData"]["categories"]}
        scored = {k: v["score"] for k, v in cats.items()
                  if v["score"] is not None}
        self.assertTrue(scored)
        best = max(scored, key=scored.get)
        worst = min(scored, key=scored.get)
        self.assertTrue(r["strongest_category"].startswith(cats[best]["name"]))
        self.assertTrue(r["weakest_category"].startswith(cats[worst]["name"]))
        # The label carries the score, as the reference summary does.
        self.assertIn(f"{scored[best]:.1f}", r["strongest_category"])

    def test_extremes_are_blank_when_nothing_scored(self):
        from fundos.assessment import phase1
        self.assertEqual(phase1.category_extremes(
            [{"name": "Team", "score": None}]), ("", ""))

    def test_ask_amount_prefers_the_founders_entered_target(self):
        from fundos.profile.targets import DealTargets

        DealTargets.objects.create(
            tenant_id=self.world["a"]["tenant"].id, deal_id=self.deal.id,
            company=self.company, target_raise_value=Decimal("7"),
            target_raise_ccy="USD")
        r = self._get().data
        self.assertEqual(r["ask_amount"], "USD 7")
        # capital_raised stays what the assessment banded category D against.
        self.assertEqual(r["capital_raised"], "USD 5M")

    def test_scorecard_headline_is_the_engine_s_own_number(self):
        """Nothing here recomputes the score — it reads what run_scoring wrote."""
        r = self._get()
        card = r.data["dealScorecardData"]
        self.assertEqual(Decimal(str(card["system_score"])),
                         self.assessment.overall_score)
        self.assertEqual(card["system_rating"], self.assessment.rating_band)

    def test_nested_rollup_agrees_with_the_persisted_overall(self):
        """The tree is rolled up by the engine's own primitive.

        The screen draws levels nothing persists. If they were computed by a
        second, looser calculation the sub-items on screen would not add up to
        the headline beside them.
        """
        from fundos.assessment import phase1

        overall, _roots = phase1.scored_tree(self.assessment)
        self.assertIsNotNone(overall)
        self.assertAlmostEqual(overall, float(self.assessment.overall_score),
                               places=2)

    def test_tree_is_nested_and_carries_weights(self):
        card = self._get().data["dealScorecardData"]
        team = next(c for c in card["categories"] if c["ref"] == "A")
        self.assertEqual(team["declaredWeight"], 20)
        self.assertEqual(team["name"], "Team")
        founder_profile = next(s for s in team["children"] if s["ref"] == "A.1")
        # The sub-item carries its configured label, not its code — the screen
        # shows "Founder Profile", not "A.1".
        self.assertEqual(founder_profile["name"], "Founder Profile")
        leaves = [k for k in founder_profile["children"] if k.get("inputKey")]
        self.assertTrue(leaves, "A.1 should carry scored parameters")
        leaf = next(k for k in leaves if k["inputKey"] == "TEAM_FDR_EXP")
        self.assertEqual(leaf["band"], "Excellent")
        self.assertEqual(leaf["ref"], "A.1.d")
        self.assertIn("weightWithinParent", leaf["weights"])

    def test_unassessed_siblings_redistribute_weight(self):
        """A blank does not score zero — it makes its siblings matter more."""
        card = self._get().data["dealScorecardData"]
        team = next(c for c in card["categories"] if c["ref"] == "A")
        a1 = next(s for s in team["children"] if s["ref"] == "A.1")
        scored = [k for k in a1["children"]
                  if k.get("inputKey") and k["score"] is not None]
        applied = sum(k["weights"]["weightWithinParent"] for k in scored)
        self.assertAlmostEqual(applied, 100.0, places=4)
        # Declared shares are 20 each across five children; with fewer scored,
        # the applied share must exceed the declared one.
        self.assertGreater(scored[0]["weights"]["weightWithinParent"],
                           scored[0]["weights"]["declaredWeight"])

    def test_empty_subitems_are_not_rendered_as_parameters(self):
        """A sub-item nobody was asked about is an empty branch, not a row."""
        card = self._get().data["dealScorecardData"]
        for category in card["categories"]:
            for sub in category["children"]:
                for kid in sub.get("children") or []:
                    if "inputKey" in kid:
                        self.assertTrue(kid["inputKey"],
                                        "a leaf must name its parameter")


    def test_narrative_reads_object_sections_structured_prose(self):
        """The two sections worth quoting are object sections: their prose is
        in `structured`, keyed by the schema's field names, and `content` is
        empty. Reading `content` alone emptied this block on every real
        company."""
        from fundos.profile.models import ProfileSection
        from fundos.profile.services import get_or_create_profile

        profile = get_or_create_profile(self.company, user=self.founder)
        ProfileSection.objects.update_or_create(
            profile=profile, section_key="company_overview",
            defaults={"tenant_id": self.company.tenant_id, "content": "",
                      "structured": {"description_of_business":
                                     "A B2B marketplace for tyres."},
                      "is_active": True})
        ProfileSection.objects.update_or_create(
            profile=profile, section_key="investment_thesis",
            defaults={"tenant_id": self.company.tenant_id, "content": "",
                      "structured": {"opportunity_explanation":
                                     "A large fragmented market."},
                      "is_active": True})

        narrative = self._get().data["executiveSummaryData"]["narrative"]
        self.assertIn("A B2B marketplace for tyres.", narrative)
        self.assertIn("A large fragmented market.", narrative)

    def test_executive_summary_names_the_cohort_the_score_used(self):
        summary = self._get().data["executiveSummaryData"]
        labels = [t["label"] for t in summary["tags"]]
        self.assertIn("Sector: Ecommerce", labels)
        self.assertIn("Sub-sector: B2B Ecommerce", labels)
        self.assertIn("Stage: Series A", labels)


class DiligenceFindingsTests(Phase1Base):

    def test_low_confidence_is_a_finding(self):
        findings = {f["inputKey"]: f
                    for f in self._get().data["diligenceFindingsData"]}
        self.assertIn("TEAM_ADVISORS", findings)
        self.assertEqual(findings["TEAM_ADVISORS"]["confidence"], "low")

    def test_unanswered_parameters_are_findings_and_are_not_evidenced(self):
        """The rows worth asking about have no ParameterValue at all."""
        findings = {f["inputKey"]: f
                    for f in self._get().data["diligenceFindingsData"]}
        # Nothing was extracted for category B; every one of its parameters
        # should surface rather than being invisible.
        unevidenced = [f for f in findings.values()
                       if f["evidenceTier"] == "Not Evidenced"]
        self.assertTrue(unevidenced)
        # Every one carries an actionable ask naming the document that would
        # answer it. It is NOT counted as a data gap — see
        # TriggerSeparationTests for why the two triggers stay distinct.
        self.assertTrue(all(f["ask"] for f in unevidenced))

    def test_an_answered_high_confidence_row_is_not_a_finding(self):
        findings = {f["inputKey"]: f
                    for f in self._get().data["diligenceFindingsData"]}
        self.assertNotIn("TEAM_FDR_EXP", findings)

    def test_the_extraction_ask_becomes_the_ask(self):
        findings = {f["inputKey"]: f
                    for f in self._get().data["diligenceFindingsData"]}
        self.assertIn("advisory board roster",
                      findings["TEAM_ADVISORS"]["ask"])

    def test_a_recorded_contradiction_lands_on_the_row_it_names(self):
        """§2.4.5 — a ref row two bands below its sibling is a finding."""
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        # TEAM_CXO_SEATS is the cross-check on A.2; score it Poor against a
        # scored sibling, which is what the rule looks for.
        ParameterValue.objects.create(
            assessment=self.assessment, input_key="TEAM_CXO_SEATS",
            raw_value="0", category="A", is_reference_only=True,
            source_type="document", source_tier=1, confidence=Decimal("0.9"))
        ParameterValue.objects.create(
            assessment=self.assessment, input_key="ANC_CXO_PRODUCT",
            band="Excellent", category="A", source_type="document",
            source_tier=1, confidence=Decimal("0.9"))
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

        contradicted = [f for f in self._get().data["diligenceFindingsData"]
                        if f["contradictions"]]
        self.assertTrue(contradicted,
                        "a recorded contradiction must reach the screen")
        self.assertTrue(all(f["severity"] == "high" for f in contradicted))


class BandRecommendationTests(Phase1Base):

    def test_only_fair_and_good_rows_are_recommended(self):
        items = self._get().data["bandRecommendationsData"]
        self.assertTrue(items)
        self.assertTrue(all(i["currentBand"] in ("Fair", "Good")
                            for i in items))

    def test_target_is_the_next_highest_band(self):
        items = {i["inputKey"]: i
                 for i in self._get().data["bandRecommendationsData"]}
        self.assertEqual(items["TEAM_COFDR_YRS"]["currentBand"], "Good")
        self.assertEqual(items["TEAM_COFDR_YRS"]["targetBand"], "Excellent")

    def test_description_quotes_the_published_cut_point(self):
        """The proof point IS the rule. Inventing advice means inventing a
        threshold, which is what a rubric exists to prevent."""
        items = {i["inputKey"]: i
                 for i in self._get().data["bandRecommendationsData"]}
        description = items["TEAM_COFDR_YRS"]["description"]
        # Series A cut-points for TEAM_COFDR_YRS are [6, 4, 2]; Excellent is 6.
        self.assertIn("6", description)
        self.assertIn("Series A", description)

    def test_impact_is_a_real_re_roll_not_an_estimate(self):
        """A leaf's effect is not weight times delta — every level above it
        redistributes around whatever is unscored beside it."""
        from fundos.assessment import phase1

        items = {i["inputKey"]: i
                 for i in self._get().data["bandRecommendationsData"]}
        row = items["TEAM_COFDR_YRS"]
        recomputed = phase1.rescore_with(
            self.assessment, self.assessment.tenant_id, "TEAM_COFDR_YRS", 9.0)
        self.assertAlmostEqual(row["overallIfReached"], round(recomputed, 2),
                               places=2)
        self.assertGreater(row["overallImpact"], 0)

    def test_ordered_by_what_it_is_worth(self):
        items = self._get().data["bandRecommendationsData"]
        impacts = [i["overallImpact"] or 0 for i in items]
        self.assertEqual(impacts, sorted(impacts, reverse=True))


class Phase1OverrideTests(Phase1Base):

    def test_override_recalculates_parent_category_and_overall(self):
        before = float(self.assessment.overall_score)
        r = self._override(parameter_ref="A.1.d", override_score=3.0,
                           reason="Experience is not in this sector.")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertTrue(r.data["success"])

        after = r.data["recalculated_total_score"]
        self.assertLess(after, before)
        self.assertEqual(r.data["updated_band"],
                         self._reloaded().rating_band)

        # The whole chain moved, not just the leaf.
        card = self._get().data["dealScorecardData"]
        team = next(c for c in card["categories"] if c["ref"] == "A")
        a1 = next(s for s in team["children"] if s["ref"] == "A.1")
        leaf = next(k for k in a1["children"]
                    if k.get("inputKey") == "TEAM_FDR_EXP")
        self.assertEqual(leaf["score"], 3.0)
        self.assertTrue(leaf["isOverridden"])
        self.assertIsNotNone(a1["score"])
        self.assertIsNotNone(team["score"])
        self.assertEqual(card["system_score"], after)

    def test_the_rule_s_own_verdict_survives_the_override(self):
        """An override is a recorded disagreement, not a number somebody
        changed. The system band must still be readable beside it."""
        r = self._override(parameter_ref="A.1.d", override_score=3.0,
                           reason="Experience is not in this sector.")
        self.assertEqual(r.data["systemBand"], "Excellent")
        self.assertEqual(r.data["systemScore"], 9.0)
        self.assertEqual(r.data["parameterBand"], "Poor")

    def test_empty_reason_is_rejected(self):
        r = self._override(parameter_ref="A.1.d", override_score=8.0,
                           reason="   ")
        self.assertEqual(r.status_code, 400)
        self.assertIn("reason", r.data.get("fields", {}))

    def test_missing_reason_key_is_rejected(self):
        r = self._override(parameter_ref="A.1.d", override_score=8.0)
        self.assertEqual(r.status_code, 400)

    def test_unknown_parameter_is_404(self):
        r = self._override(parameter_ref="Z.9.z", override_score=8.0,
                           reason="because")
        self.assertEqual(r.status_code, 404)

    def test_score_outside_the_scale_is_rejected(self):
        r = self._override(parameter_ref="A.1.d", override_score=11.0,
                           reason="because")
        self.assertEqual(r.status_code, 400)

    def test_override_is_recorded_in_the_review_trail(self):
        from fundos.assessment.models import ReviewLog

        self._override(parameter_ref="A.1.d", override_score=3.0,
                       reason="Experience is not in this sector.")
        entry = ReviewLog.objects.filter(
            assessment=self.assessment, field_or_topic="TEAM_FDR_EXP").first()
        self.assertIsNotNone(entry, "an override must leave a trail entry")
        self.assertEqual(entry.status, "closed")
        self.assertIn("not in this sector", entry.founder_reason)

    def test_ten_is_reachable_only_here_and_is_flagged(self):
        r = self._override(parameter_ref="A.1.d", override_score=10.0,
                           reason="Better than the rubric can express.")
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.data["beyondAutomatedCeiling"])

    def test_input_key_addresses_a_row_exactly(self):
        r = self._override(parameter_ref="TEAM_FDR_EXP", override_score=5.0,
                           reason="Recalibrated against the sector.")
        self.assertEqual(r.status_code, 200, r.data)
        self.assertEqual(r.data["inputKey"], "TEAM_FDR_EXP")

    def _reloaded(self):
        from fundos.assessment.models import Assessment
        return Assessment.objects.get(id=self.assessment.id)


class CanonicalBandTests(Phase1Base):
    """One band vocabulary — the capitalised one the engine writes."""

    def test_every_band_on_the_response_is_canonical(self):
        from fundos.engines.deal_assessment import BAND_SCORES

        allowed = set(BAND_SCORES) | {""}
        body = self._get().data

        def walk(node):
            if "band" in node:
                self.assertIn(node["band"], allowed, node.get("ref"))
            if "systemBand" in node:
                self.assertIn(node["systemBand"], allowed, node.get("ref"))
            for kid in node.get("children") or []:
                walk(kid)

        for category in body["dealScorecardData"]["categories"]:
            walk(category)
        for f in body["diligenceFindingsData"]:
            self.assertIn(f["band"], allowed)
        for r in body["bandRecommendationsData"]:
            self.assertIn(r["currentBand"], allowed)
            self.assertIn(r["targetBand"], allowed)

    def test_a_legacy_lowercase_band_is_still_read(self):
        """Rows written by the older override path are still in live
        databases; a 'good' left unread would silently drop a real parameter
        out of the recommendations."""
        from fundos.assessment.models import ParameterValue

        pv = ParameterValue.objects.get(assessment=self.assessment,
                                        input_key="TEAM_COFDR_YRS")
        ParameterValue.objects.filter(pk=pv.pk).update(band="good",
                                                       is_overridden=True)
        items = {i["inputKey"]: i
                 for i in self._get().data["bandRecommendationsData"]}
        self.assertIn("TEAM_COFDR_YRS", items)
        self.assertEqual(items["TEAM_COFDR_YRS"]["currentBand"], "Good")

    def test_override_writes_the_canonical_vocabulary(self):
        from fundos.assessment.models import ParameterValue

        self._override(parameter_ref="A.1.d", override_score=5.0,
                       reason="Recalibrated against the sector.")
        pv = ParameterValue.objects.get(assessment=self.assessment,
                                        input_key="TEAM_FDR_EXP")
        self.assertEqual(pv.band, "Fair")


class ConfiguredThresholdTests(Phase1Base):
    """Nothing on this screen restates a number that lives in config."""

    def test_band_scores_come_from_config_not_from_a_literal(self):
        """Retune the scoring key and the recommendation must follow it."""
        from fundos.assessment.models import ConfigConstant
        from fundos.assessment.services import band_score

        self.assertEqual(band_score("Excellent"), 9.0)
        ConfigConstant.objects.create(tenant_id=None, key="score_excellent",
                                      value=Decimal("8.5"), version=1,
                                      is_active=True)
        self.assertEqual(band_score("Excellent"), 8.5)

        items = {i["inputKey"]: i
                 for i in self._get().data["bandRecommendationsData"]}
        self.assertEqual(items["TEAM_COFDR_YRS"]["targetScore"], 8.5)

    def test_confidence_uses_the_extraction_floor_only(self):
        from fundos.assessment import phase1
        from fundos.assessment.extraction import CONFIDENCE_FLOOR
        from fundos.assessment.models import ParameterValue

        pv = ParameterValue.objects.get(assessment=self.assessment,
                                        input_key="TEAM_INST_INV")
        pv.confidence = Decimal(str(CONFIDENCE_FLOOR - 0.01))
        self.assertEqual(phase1.confidence_label(pv), "low")
        pv.confidence = Decimal(str(CONFIDENCE_FLOOR))
        self.assertEqual(phase1.confidence_label(pv), "high")


class TriggerSeparationTests(Phase1Base):
    """The four triggers stay distinguishable from one another."""

    def test_an_unanswered_row_is_not_evidenced_and_is_not_a_data_gap(self):
        """Counting it as both would make the two triggers indistinguishable.

        It still reaches the screen, and still carries an ask naming the
        document that would answer it.
        """
        findings = {f["inputKey"]: f
                    for f in self._get().data["diligenceFindingsData"]}
        # Nothing was extracted for A.1.a.
        row = findings["ANC_FDR_EDU"]
        self.assertEqual(row["evidenceTier"], "Not Evidenced")
        self.assertEqual(row["dataGaps"], [])
        self.assertTrue(row["ask"], "an unanswered row still needs an ask")
        self.assertIn("No value was established", row["observation"])

    def test_a_stated_gap_is_the_extraction_ask_verbatim(self):
        findings = {f["inputKey"]: f
                    for f in self._get().data["diligenceFindingsData"]}
        row = findings["TEAM_ADVISORS"]
        self.assertEqual(
            row["dataGaps"],
            ["Ask the founder for the advisory board roster."])


class ScalabilityTests(Phase1Base):
    """A page load must not cost more as the scoring model grows."""

    def test_query_count_does_not_scale_with_the_model(self):
        """Before `Reference`, one GET issued 412 queries for 62 leaves —
        two per leaf for the rubric and anchor, three per rolled-up node for
        the scoring key, a full tree rebuild per recommendation, and two per
        unanswered row inside `source_for`. All of that is page-wide config."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            r = self._get()
        self.assertEqual(r.status_code, 200)
        leaves = []

        def walk(n):
            if n.get("inputKey"):
                leaves.append(n)
            for k in n.get("children") or []:
                walk(k)
        for c in r.data["dealScorecardData"]["categories"]:
            walk(c)

        self.assertGreater(len(leaves), 40, "expected a full-size model")
        self.assertLess(len(ctx.captured_queries), 60,
                        f"{len(ctx.captured_queries)} queries for "
                        f"{len(leaves)} leaves — the N+1 is back")

    def test_a_ready_get_writes_nothing(self):
        """Polling must not mutate the assessment."""
        from django.db import connection
        from django.test.utils import CaptureQueriesContext

        with CaptureQueriesContext(connection) as ctx:
            self._get()
        writes = [q for q in ctx.captured_queries
                  if q["sql"].strip()[:6].upper() in ("INSERT", "UPDATE",
                                                      "DELETE")]
        self.assertEqual(writes, [], "a read endpoint wrote to the database")

    def test_repeated_gets_are_stable_and_create_nothing(self):
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.core.models import GenerationJob

        before = (Assessment.objects.count(), ParameterValue.objects.count(),
                  GenerationJob.objects.count())
        scores = [self._get().data["overall_score"] for _ in range(5)]
        after = (Assessment.objects.count(), ParameterValue.objects.count(),
                 GenerationJob.objects.count())

        self.assertEqual(len(set(scores)), 1, f"unstable across polls: {scores}")
        self.assertEqual(before, after, "a poll created rows")


class IdempotentGenerationTests(Phase1Base):
    """Repeated page loads must not queue repeated runs."""

    def _company_b(self):
        return (self.world["b"]["company"],
                auth_headers(self.world["b"]["founder"]))

    def test_an_in_flight_job_blocks_a_second_run(self):
        """The frontend polls while it waits. Without this guard every poll
        would queue another extraction, so a user watching a spinner would
        multiply the cost of their own wait."""
        from fundos.core.models import GenerationJob
        from fundos.profile.views import _default_deal

        company, headers = self._company_b()
        deal = _default_deal(company, self.world["b"]["founder"])
        # Stand a run up as if a worker had it.
        GenerationJob.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, kind="assessment",
            status="running")
        before = GenerationJob.objects.filter(kind="assessment").count()

        for _ in range(4):
            r = self.client.get(
                f"/api/v1/companies/{company.id}/fundraising/phase1",
                **headers)
            self.assertEqual(r.status_code, 202)
            self.assertEqual(r.data["status"], "generating")

        after = GenerationJob.objects.filter(kind="assessment").count()
        self.assertEqual(before, after,
                         "a poll queued a duplicate assessment run")

    def test_a_finished_job_does_not_block_a_later_one(self):
        """Only queued/running block. A succeeded or failed run must not wedge
        the company out of ever being reassessed."""
        from fundos.core.models import GenerationJob
        from fundos.profile.views import _default_deal

        company, headers = self._company_b()
        deal = _default_deal(company, self.world["b"]["founder"])
        GenerationJob.objects.create(
            tenant_id=deal.tenant_id, deal_id=deal.id, kind="assessment",
            status="succeeded")
        r = self.client.get(
            f"/api/v1/companies/{company.id}/fundraising/phase1", **headers)
        self.assertIn(r.status_code, (200, 202, 502))
        self.assertNotEqual(r.data.get("status"), "generating")


class LifecycleTests(Phase1Base):
    """The client must be able to tell the four states apart."""

    def test_ready_reports_ready(self):
        r = self._get()
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.data["status"], "ready")

    def test_an_assessment_with_no_evidence_reports_no_evidence(self):
        self.assessment.parameter_values.all().delete()
        r = self._get()
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data["status"], "no_evidence")
        self.assertIsNone(r.data["overall_score"])

    def test_an_empty_assessment_with_a_running_job_reports_generating(self):
        from fundos.core.models import GenerationJob

        self.assessment.parameter_values.all().delete()
        GenerationJob.objects.create(
            tenant_id=self.assessment.tenant_id,
            deal_id=self.assessment.deal_id, kind="assessment",
            status="running")
        r = self._get()
        self.assertEqual(r.status_code, 202)
        self.assertEqual(r.data["status"], "generating")

    def test_an_empty_assessment_with_a_failed_job_reports_failed(self):
        from fundos.core.models import GenerationJob

        self.assessment.parameter_values.all().delete()
        GenerationJob.objects.create(
            tenant_id=self.assessment.tenant_id,
            deal_id=self.assessment.deal_id, kind="assessment",
            status="failed", error="gemini 429: every key is rate-limited")
        r = self._get()
        self.assertEqual(r.status_code, 502)
        self.assertEqual(r.data["status"], "failed")
        self.assertIn("rate-limited", r.data["reason"])
        # A failure must never look like a scored deal.
        self.assertIsNone(r.data["overall_score"])
        self.assertEqual(r.data["dealScorecardData"], {})

    def test_no_state_ever_returns_a_fabricated_score(self):
        from fundos.core.models import GenerationJob

        self.assessment.parameter_values.all().delete()
        for job_status in ("running", "failed", None):
            GenerationJob.objects.filter(kind="assessment").delete()
            if job_status:
                GenerationJob.objects.create(
                    tenant_id=self.assessment.tenant_id,
                    deal_id=self.assessment.deal_id, kind="assessment",
                    status=job_status)
            r = self._get()
            self.assertNotEqual(r.status_code, 200)
            self.assertIsNone(r.data["overall_score"])
            self.assertIsNone(r.data["deal_rating"])
            self.assertEqual(r.data["diligenceFindingsData"], [])


class MultiCompanyIsolationTests(Phase1Base):
    """Two scored companies must not bleed into one another."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import Assessment, ParameterValue
        from fundos.assessment.services import run_scoring

        # A second company, in the OTHER tenant, scored differently.
        self.company_b = self.world["b"]["company"]
        self.assessment_b = Assessment.objects.create(
            company=self.company_b, deal=self.world["b"]["deal"],
            tenant_id=self.world["b"]["tenant"].id,
            deal_stage="Growth", sector="Fintech", sub_sector="Payments",
            capital_raised_usd_mn=Decimal("25"))
        for key, value in (("TEAM_FDR_EXP", "3"), ("TEAM_COFDR_YRS", "1")):
            ParameterValue.objects.create(
                assessment=self.assessment_b, input_key=key, raw_value=value,
                category="A", source_type="document", source_tier=1,
                confidence=Decimal("0.9"))
        run_scoring(self.assessment_b)
        self.assessment_b.refresh_from_db()

    def _get_b(self):
        return self.client.get(
            f"/api/v1/companies/{self.company_b.id}/fundraising/phase1",
            **auth_headers(self.world["b"]["founder"]))

    def test_each_company_sees_only_its_own_assessment(self):
        a, b = self._get().data, self._get_b().data

        self.assertEqual(a["companyId"], str(self.company.id))
        self.assertEqual(b["companyId"], str(self.company_b.id))
        self.assertEqual(a["assessmentId"], str(self.assessment.id))
        self.assertEqual(b["assessmentId"], str(self.assessment_b.id))
        self.assertNotEqual(a["assessmentId"], b["assessmentId"])

    def test_metadata_and_scores_do_not_bleed(self):
        a, b = self._get().data, self._get_b().data

        self.assertEqual(a["sector"], "Ecommerce")
        self.assertEqual(b["sector"], "Fintech")
        self.assertEqual(a["deal_stage"], "Series A")
        self.assertEqual(b["deal_stage"], "Growth")
        self.assertNotEqual(a["overall_score"], b["overall_score"])

    def test_evidence_is_scoped_to_its_own_assessment(self):
        """B answered two parameters; A answered four different ones."""
        def leaves(payload):
            out = {}

            def walk(n):
                if n.get("inputKey") and n.get("score") is not None:
                    out[n["inputKey"]] = n["score"]
                for k in n.get("children") or []:
                    walk(k)
            for c in payload["dealScorecardData"]["categories"]:
                walk(c)
            return out

        a, b = leaves(self._get().data), leaves(self._get_b().data)
        self.assertEqual(set(b), {"TEAM_FDR_EXP", "TEAM_COFDR_YRS"})
        # A answered two parameters B never did.
        self.assertIn("TEAM_INST_INV", a)
        self.assertIn("TEAM_ADVISORS", a)
        self.assertNotIn("TEAM_INST_INV", b)
        self.assertNotIn("TEAM_ADVISORS", b)
        # Same parameter, different evidence, different score.
        self.assertNotEqual(a["TEAM_FDR_EXP"], b["TEAM_FDR_EXP"])

    def test_an_override_on_one_company_leaves_the_other_alone(self):
        before_b = self._get_b().data["overall_score"]
        r = self._override(parameter_ref="A.1.d", override_score=3.0,
                           reason="Scoped to company A only.")
        self.assertEqual(r.status_code, 200)
        after_b = self._get_b().data["overall_score"]
        self.assertEqual(before_b, after_b,
                         "an override changed another company's score")

    def test_a_reference_is_per_request_not_shared(self):
        """Module-level caching of config would leak one company's tenant
        overrides into another's scorecard."""
        from fundos.assessment import phase1

        ref_a = phase1.Reference(self.assessment)
        ref_b = phase1.Reference(self.assessment_b)
        self.assertIsNot(ref_a, ref_b)
        self.assertIsNot(ref_a.params, ref_b.params)
        self.assertEqual(ref_a.assessment.id, self.assessment.id)
        self.assertEqual(ref_b.assessment.id, self.assessment_b.id)


class OverrideEdgeCaseTests(Phase1Base):

    def test_negative_score_is_rejected(self):
        r = self._override(parameter_ref="A.1.d", override_score=-1.0,
                           reason="negative")
        self.assertEqual(r.status_code, 400)

    def test_non_numeric_score_is_rejected(self):
        r = self._override(parameter_ref="A.1.d", override_score="abc",
                           reason="not a number")
        self.assertEqual(r.status_code, 400)

    def test_missing_parameter_ref_is_rejected(self):
        r = self._override(override_score=5.0, reason="no ref")
        self.assertEqual(r.status_code, 400)

    def test_repeated_override_of_the_same_parameter_is_last_write_wins(self):
        from fundos.assessment.models import ParameterValue

        self._override(parameter_ref="A.1.d", override_score=3.0,
                       reason="first")
        second = self._override(parameter_ref="A.1.d", override_score=8.0,
                                reason="second, after new information")
        self.assertEqual(second.status_code, 200)

        pv = ParameterValue.objects.get(assessment=self.assessment,
                                        input_key="TEAM_FDR_EXP")
        self.assertEqual(float(pv.score), 8.0)
        self.assertEqual(pv.override_comment, "second, after new information")
        # Exactly one value row — an override edits, never accumulates.
        self.assertEqual(ParameterValue.objects.filter(
            assessment=self.assessment, input_key="TEAM_FDR_EXP").count(), 1)
        # But BOTH decisions are in the trail.
        from fundos.assessment.models import ReviewLog
        self.assertEqual(ReviewLog.objects.filter(
            assessment=self.assessment,
            field_or_topic="TEAM_FDR_EXP").count(), 2)

    def test_multiple_overrides_compound_through_the_engine(self):
        base = float(self.assessment.overall_score)
        first = self._override(parameter_ref="A.1.d", override_score=3.0,
                               reason="one").data["recalculated_total_score"]
        second = self._override(parameter_ref="A.1.c", override_score=3.0,
                                reason="two").data["recalculated_total_score"]
        self.assertLess(first, base)
        self.assertLess(second, first)

        card = self._get().data["dealScorecardData"]
        team = next(c for c in card["categories"] if c["ref"] == "A")
        a1 = next(s for s in team["children"] if s["ref"] == "A.1")
        overridden = [k for k in a1["children"] if k.get("isOverridden")]
        self.assertEqual(len(overridden), 2)
        self.assertAlmostEqual(a1["score"], 3.0, places=4)

    def test_the_system_verdict_survives_every_override(self):
        from fundos.assessment.models import ParameterValue

        self._override(parameter_ref="A.1.d", override_score=3.0, reason="x")
        self._override(parameter_ref="A.1.d", override_score=10.0, reason="y")
        pv = ParameterValue.objects.get(assessment=self.assessment,
                                        input_key="TEAM_FDR_EXP")
        self.assertEqual(pv.system_band, "Excellent")
        self.assertEqual(float(pv.system_score), 9.0)


class Phase1AccessTests(Phase1Base):

    def test_unauthenticated_is_refused(self):
        r = self.client.get(
            f"/api/v1/companies/{self.company.id}/fundraising/phase1")
        self.assertIn(r.status_code, (401, 403))

    def test_another_tenant_cannot_see_the_scorecard(self):
        """404, not 403 — an endpoint must not confirm a company exists to
        someone who may not see it."""
        outsider = auth_headers(self.world["b"]["founder"])
        r = self.client.get(
            f"/api/v1/companies/{self.company.id}/fundraising/phase1",
            **outsider)
        self.assertEqual(r.status_code, 404)


class AutoGenerationTests(Phase1Base):
    """The client never has to generate an assessment itself."""

    def _company_b(self):
        return (self.world["b"]["company"],
                auth_headers(self.world["b"]["founder"]))

    def test_no_assessment_starts_the_existing_pipeline(self):
        """A 404 telling the client to POST elsewhere would make the front end
        responsible for a backend sequencing detail."""
        from fundos.core.models import GenerationJob

        company, headers = self._company_b()
        r = self.client.get(
            f"/api/v1/companies/{company.id}/fundraising/phase1", **headers)

        self.assertNotEqual(r.status_code, 404)
        self.assertIn(r.status_code, (200, 202, 502))
        self.assertTrue(
            GenerationJob.objects.filter(kind="assessment").exists(),
            "the existing generation job mechanism must have been used")

    def test_the_contract_keys_are_present_even_before_scoring(self):
        """A client destructuring the response must not have to branch on
        whether scoring has finished."""
        company, headers = self._company_b()
        r = self.client.get(
            f"/api/v1/companies/{company.id}/fundraising/phase1", **headers)
        if r.status_code == 502:
            self.skipTest("generation failed in this environment")
        for key in CONTRACT_KEYS:
            self.assertIn(key, r.data)
        self.assertEqual(r.data["company"], company.name)

    def test_a_pending_response_never_pretends_to_be_a_score(self):
        company, headers = self._company_b()
        r = self.client.get(
            f"/api/v1/companies/{company.id}/fundraising/phase1", **headers)
        if r.status_code == 200:
            self.skipTest("generation completed inline; nothing pending")
        if r.status_code == 502:
            self.assertIn("could not be", r.data["detail"])
            return
        self.assertEqual(r.status_code, 202)
        self.assertIsNone(r.data["overall_score"])
        self.assertIsNone(r.data["deal_rating"])
        self.assertEqual(r.data["dealScorecardData"], {})
        self.assertEqual(r.data["bandRecommendationsData"], [])

    def test_generation_task_uses_the_real_job_service(self):
        """Regression: the task called three functions that do not exist on
        `fundos.core.services.jobs`, so every generation died at the first
        line and no deal ever got an assessment."""
        from fundos.assessment.tasks import generate_assessment_task
        from fundos.core.services import jobs

        deal = self.world["b"]["deal"]
        job = jobs.create_job(deal, "assessment", self.world["b"]["founder"])
        generate_assessment_task(
            str(deal.tenant_id), str(deal.id),
            str(self.world["b"]["founder"].id), job_id=str(job.id))
        job.refresh_from_db()
        self.assertEqual(job.status, "succeeded", job.error)
        self.assertIsNotNone(job.finished_at)
        self.assertIsNotNone(job.artifact_id)


class Phase1AccessExtraTests(Phase1Base):

    def test_prototype_style_job_id_does_not_resolve(self):
        """A 32-char hex id with no dashes is the PROTOTYPE's job id, not a
        production company id, and must not silently resolve to anything."""
        from django.urls import Resolver404, resolve

        with self.assertRaises(Resolver404):
            resolve("/api/v1/companies/b461cf5a245842b9b5d73cdbc3b1eb79"
                    "/fundraising/phase1")

    def test_the_route_is_registered_under_the_contract_path(self):
        from django.urls import resolve

        match = resolve(f"/api/v1/companies/{self.company.id}"
                        f"/fundraising/phase1")
        self.assertEqual(match.func.view_class.__name__,
                         "FundraisingPhase1View")
