"""The scorecard panel: what to ask, and the override the chat may propose.

The assessment side had no chat at all — asking the profile chat why Team
scored 6.87 got an answer about the company, because it has never seen a
parameter. Three things are asserted here.

    THE QUESTIONS DIFFER BY LEVEL. A grandchild scores, so its questions are
    about evidence and bands. A child is a group: which of its rows is
    holding it back. A category is a fifth of a rating: where the largest
    lever is. "What would settle this category?" is a category error.

    THE ARITHMETIC IS THE ENGINE'S. "If this were settled, what happens to
    the score?" is computed by rerunning the real roll-up with the row
    substituted — so it includes the redistribution nobody does in their
    head — and never taken from a model.

    THE CHAT NEVER MOVES A SCORE. It proposes; a person applies through the
    override endpoint, which already demands a reason and keeps the machine's
    own answer in its own columns.

Everything runs against the real seeded scoring model, not a cut-down
fixture: the rules have to hold for whatever a live scorecard contains.
"""
from decimal import Decimal
from unittest.mock import patch

from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import auth_headers, make_world


class ScoredDeal(TestCase):
    """Category A scored from evidence, with one row left unevidenced."""

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
        self.base = f"/api/v1/deals/{self.deal.id}/assessment"

        self.assessment = Assessment.objects.create(
            company=self.company, deal=self.deal,
            tenant_id=self.world["a"]["tenant"].id,
            deal_stage="Series A", sector="Ecommerce",
            sub_sector="B2B Ecommerce", capital_raised_usd_mn=Decimal("5"))

        for key, value, evidence in (
                ("TEAM_FDR_EXP", "12", "deck, slide 3"),
                ("TEAM_COFDR_YRS", "5", "deck, slide 3"),
                ("TEAM_INST_INV", "2", "deck, slide 4"),
                ("TEAM_ADVISORS", "1", "")):
            ParameterValue.objects.create(
                assessment=self.assessment, input_key=key, raw_value=value,
                category="A", source_type="document",
                source_detail=evidence, confidence=Decimal("0.8"),
                justification=f"Read directly for {key}.")
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _kinds(self, ref):
        from fundos.assessment import suggestions

        return [s["kind"] for s in suggestions.for_ref(self.assessment, ref)]

    def _ref_of(self, input_key):
        pv = self.assessment.parameter_values.get(input_key=input_key)
        return pv.ref_code or input_key


class TheQuestionsDifferByLevel(ScoredDeal):

    def test_a_category_is_asked_where_its_lever_is(self):
        kinds = self._kinds("A")
        self.assertTrue(kinds)
        self.assertIn("weakest", kinds)

    def test_a_category_is_never_asked_what_would_settle_it(self):
        """Nothing settles a category. Its rows do."""
        self.assertNotIn("settle", self._kinds("A"))

    def test_a_child_is_asked_about_the_rows_beneath_it(self):
        from fundos.assessment import hierarchy as H

        child = next(ref for ref, level in H.LEVELS.items()
                     if level == "child" and ref.startswith("A."))
        kinds = self._kinds(child)
        self.assertTrue(kinds)
        self.assertFalse({"weakest", "gaps", "evidence"}.isdisjoint(kinds))

    def test_a_scoring_row_is_asked_what_would_settle_it(self):
        self.assertIn("settle", self._kinds(self._ref_of("TEAM_FDR_EXP")))

    def test_an_unknown_ref_suggests_nothing(self):
        """A panel showing questions about a row that does not exist is
        worse than one showing none."""
        self.assertEqual(self._kinds("Z.9.z"), [])

    def test_every_category_answers(self):
        from fundos.assessment import hierarchy as H
        from fundos.assessment.suggestions import for_assessment

        out = for_assessment(self.assessment)
        self.assertEqual(set(out), set(H.CATEGORY_CODES))


class AnUnevidencedRowIsADifferentProblem(ScoredDeal):

    def test_a_row_with_no_source_is_asked_about_its_evidence(self):
        self.assertIn("evidence", self._kinds(self._ref_of("TEAM_ADVISORS")))

    def test_an_evidenced_row_is_not(self):
        self.assertNotIn("evidence", self._kinds(self._ref_of("TEAM_FDR_EXP")))


class TheArithmeticIsTheEnginesOwn(ScoredDeal):

    def test_the_impact_of_settling_a_row_is_computed(self):
        from fundos.assessment import impact

        preview, target = impact.if_settled_at(
            self.assessment, "TEAM_ADVISORS", "Good")
        self.assertEqual(target, 7)
        self.assertIsNotNone(preview["overall"]["from"])
        self.assertIsNotNone(preview["overall"]["to"])

    def test_raising_a_row_cannot_lower_the_overall(self):
        from fundos.assessment import impact

        preview, _ = impact.if_settled_at(self.assessment, "TEAM_ADVISORS",
                                          "Excellent")
        self.assertGreaterEqual(preview["overall"]["delta"], 0)

    def test_the_preview_writes_nothing(self):
        """A preview that wrote would be an override with no reason."""
        from fundos.assessment import impact

        before = self.assessment.parameter_values.get(
            input_key="TEAM_ADVISORS").score
        impact.preview(self.assessment, {"TEAM_ADVISORS": 9})
        self.assessment.refresh_from_db()
        self.assertEqual(
            self.assessment.parameter_values.get(
                input_key="TEAM_ADVISORS").score, before)

    def test_a_key_that_is_not_in_the_tree_is_reported_not_swallowed(self):
        """A preview that quietly drops its input reads as "no effect"."""
        from fundos.assessment import impact

        preview = impact.preview(self.assessment, {"NOT_A_PARAMETER": 9})
        self.assertIn("NOT_A_PARAMETER", preview["unknown"])

    def test_the_sentence_names_the_rating_when_it_changes(self):
        from fundos.assessment import impact

        self.assertEqual(impact.sentence(
            {"overall": {"from": 6.0, "to": 6.0, "delta": 0.0}}), "")
        line = impact.sentence({
            "overall": {"from": 6.0, "to": 7.4, "delta": 1.4},
            "rating": {"changed": True, "to": "Good"}})
        self.assertIn("6.00", line)
        self.assertIn("Good", line)

    def test_a_suggestion_carries_the_number_before_anyone_asks(self):
        from fundos.assessment import suggestions

        items = suggestions.for_ref(self.assessment,
                                    self._ref_of("TEAM_ADVISORS"))
        impacts = [s for s in items if s["kind"] == "impact"]
        if impacts:
            self.assertIn("overall", impacts[0]["impact"])
            self.assertIn("goes from", impacts[0]["why"])


class AnOverrideIsProposedNeverApplied(ScoredDeal):

    def _validate(self, raw):
        from fundos.assessment.qa import validate

        return validate(self.assessment, raw)

    def test_a_good_proposal_survives(self):
        out = self._validate([{"ref": self._ref_of("TEAM_ADVISORS"),
                               "score": 7,
                               "reason": "The roster lists four advisers; "
                                         "the anchor requires three for Good."}])
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["proposedScore"], 7)

    def test_the_band_is_derived_not_taken_from_the_model(self):
        """A card reading "Good 3.0" is two answers to one question."""
        out = self._validate([{"ref": self._ref_of("TEAM_ADVISORS"),
                               "score": 3, "band": "Excellent",
                               "reason": "Because."}])
        self.assertEqual(out[0]["proposedBand"], "Poor")

    def test_the_card_names_the_row_as_the_scorecard_does(self):
        """A person reads this card beside a panel headed "A.3.b Advisor
        Quality" -- it cannot say TEAM_ADVISORS twice."""
        out = self._validate([{"ref": self._ref_of("TEAM_ADVISORS"),
                               "score": 7, "reason": "Evidence found."}])
        self.assertTrue(out[0]["ref"][0].isalpha())
        self.assertIn(".", out[0]["ref"])
        self.assertNotEqual(out[0]["name"], out[0]["inputKey"])

    def test_the_impact_is_attached(self):
        out = self._validate([{"ref": self._ref_of("TEAM_ADVISORS"),
                               "score": 9, "reason": "Evidence found."}])
        self.assertIn("overall", out[0]["impact"])
        self.assertTrue(out[0]["impactSentence"])

    def test_a_row_that_is_not_on_this_assessment_is_dropped(self):
        self.assertEqual(self._validate([{"ref": "Z.9.z", "score": 7,
                                          "reason": "Because."}]), [])

    def test_a_proposal_with_no_reason_is_dropped(self):
        """The override endpoint refuses one, so the card could never apply."""
        for reason in ("", "   ", None):
            self.assertEqual(self._validate([
                {"ref": self._ref_of("TEAM_ADVISORS"), "score": 7,
                 "reason": reason}]), [])

    def test_a_score_outside_the_scale_is_dropped(self):
        for score in (-1, 11, 100, "seven", None):
            self.assertEqual(self._validate([
                {"ref": self._ref_of("TEAM_ADVISORS"), "score": score,
                 "reason": "Because."}]), [])

    def test_proposing_the_score_it_already_has_is_dropped(self):
        pv = self.assessment.parameter_values.get(input_key="TEAM_ADVISORS")
        self.assertEqual(self._validate([
            {"ref": pv.ref_code or "TEAM_ADVISORS", "score": float(pv.score),
             "reason": "No change."}]), [])

    def test_a_malformed_response_is_not_an_error(self):
        for raw in (None, "text", {"a": 1}, [None, 3, "x"]):
            self.assertEqual(self._validate(raw), [])

    def test_validating_changes_no_score(self):
        before = self.assessment.parameter_values.get(
            input_key="TEAM_ADVISORS").score
        self._validate([{"ref": self._ref_of("TEAM_ADVISORS"), "score": 9,
                         "reason": "Evidence found."}])
        self.assertEqual(
            self.assessment.parameter_values.get(
                input_key="TEAM_ADVISORS").score, before)

    def test_the_band_table_matches_the_engine(self):
        from fundos.assessment.qa import band_for_score

        for score, band in ((9, "Excellent"), (7, "Good"), (5, "Fair"),
                            (3, "Poor"), (0, "Poor"), (10, "Excellent")):
            self.assertEqual(band_for_score(score), band, score)


class ThroughTheApi(ScoredDeal):

    def test_suggestions_for_a_row(self):
        ref = self._ref_of("TEAM_FDR_EXP")
        response = self.client.get(f"{self.base}/suggestions?ref={ref}",
                                   **self.headers)
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertTrue(body["assessed"])
        self.assertTrue(body["suggestions"][ref])

    def test_suggestions_default_to_the_categories(self):
        from fundos.assessment import hierarchy as H

        body = self.client.get(f"{self.base}/suggestions",
                               **self.headers).json()
        self.assertEqual(set(body["suggestions"]), set(H.CATEGORY_CODES))

    def test_several_refs_in_one_call(self):
        ref = self._ref_of("TEAM_FDR_EXP")
        body = self.client.get(f"{self.base}/suggestions?ref=A,{ref}",
                               **self.headers).json()
        self.assertEqual(set(body["suggestions"]), {"A", ref})

    def test_the_chat_answers_and_carries_its_proposal(self):
        ref = self._ref_of("TEAM_ADVISORS")
        payload = {"answer": "The anchor requires three named advisers.",
                   "citations": [], "answered": True, "missing": [],
                   "proposedOverrides": [
                       {"ref": ref, "score": 7,
                        "reason": "Four advisers are named in the deck."}]}
        with patch("fundos.llm.adapter.llm_generate", return_value=payload):
            response = self.client.post(
                f"{self.base}/qa",
                {"question": "Why is this Poor?", "ref": ref},
                content_type="application/json", **self.headers)

        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(len(body["proposedOverrides"]), 1)
        self.assertEqual(body["proposedOverrides"][0]["proposedBand"], "Good")
        self.assertTrue(body["suggestions"])

    def test_asking_changes_no_score(self):
        ref = self._ref_of("TEAM_ADVISORS")
        before = self.assessment.parameter_values.get(
            input_key="TEAM_ADVISORS").score
        payload = {"answer": "…", "proposedOverrides": [
            {"ref": ref, "score": 9, "reason": "Because."}]}
        with patch("fundos.llm.adapter.llm_generate", return_value=payload):
            self.client.post(f"{self.base}/qa",
                             {"question": "Raise it", "ref": ref},
                             content_type="application/json", **self.headers)
        self.assertEqual(
            self.assessment.parameter_values.get(
                input_key="TEAM_ADVISORS").score, before)

    def test_a_question_is_required(self):
        response = self.client.post(f"{self.base}/qa", {"ref": "A"},
                                    content_type="application/json",
                                    **self.headers)
        self.assertEqual(response.status_code, 400)

    def test_applying_goes_through_the_existing_override_route(self):
        """One write path, one audit trail."""
        pv = self.assessment.parameter_values.get(input_key="TEAM_ADVISORS")
        response = self.client.post(
            f"{self.base}/parameters/TEAM_ADVISORS/override",
            {"score": 7, "reason": "Four advisers are named in the deck."},
            content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 200)
        pv.refresh_from_db()
        self.assertTrue(pv.is_overridden)
        self.assertEqual(float(pv.score), 7.0)
        self.assertTrue(pv.system_score is not None)

    def test_an_override_without_a_reason_is_still_refused(self):
        response = self.client.post(
            f"{self.base}/parameters/TEAM_ADVISORS/override",
            {"score": 7}, content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 400)

class TheCompanyScopedPanelHasItsOwnAddress(ScoredDeal):
    """The V2 panel is company-scoped throughout: it fetched the parameter
    from `companies/{id}/assessment/parameters/{ref}` and holds a company id,
    not a deal id. Making it resolve a deal to ask about a row it already has
    open would be friction for nothing."""

    def setUp(self):
        super().setUp()
        self.cbase = f"/api/v1/companies/{self.company.id}/assessment"

    def test_suggestions_by_company(self):
        response = self.client.get(f"{self.cbase}/suggestions?ref=A",
                                   **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["suggestions"]["A"])

    def test_suggestions_default_to_the_categories(self):
        from fundos.assessment import hierarchy as H

        body = self.client.get(f"{self.cbase}/suggestions",
                               **self.headers).json()
        self.assertEqual(set(body["suggestions"]), set(H.CATEGORY_CODES))

    def test_the_two_scopes_agree(self):
        """Same functions behind both; a different answer would mean a
        second implementation nobody is maintaining."""
        ref = self._ref_of("TEAM_ADVISORS")
        by_company = self.client.get(f"{self.cbase}/suggestions?ref={ref}",
                                     **self.headers).json()
        by_deal = self.client.get(f"{self.base}/suggestions?ref={ref}",
                                  **self.headers).json()
        self.assertEqual(by_company["suggestions"], by_deal["suggestions"])

    def test_the_chat_answers_by_company(self):
        ref = self._ref_of("TEAM_ADVISORS")
        payload = {"answer": "Because the roster is not in evidence.",
                   "proposedOverrides": [
                       {"ref": ref, "score": 7, "reason": "Four advisers."}]}
        with patch("fundos.llm.adapter.llm_generate", return_value=payload):
            response = self.client.post(
                f"{self.cbase}/qa", {"question": "Why Fair?", "ref": ref},
                content_type="application/json", **self.headers)
        self.assertEqual(response.status_code, 200)
        self.assertEqual(len(response.json()["proposedOverrides"]), 1)

    def test_the_proposal_carries_both_addresses(self):
        """`ref` for the company-scoped PATCH, `inputKey` for the
        deal-scoped POST. Whichever path the client uses, it has the
        identifier that path needs."""
        from fundos.assessment.qa import validate

        out = validate(self.assessment, [
            {"ref": self._ref_of("TEAM_ADVISORS"), "score": 7,
             "reason": "Four advisers."}])
        self.assertTrue(out[0]["ref"])
        self.assertTrue(out[0]["inputKey"])
        self.assertNotEqual(out[0]["ref"], out[0]["inputKey"])


class TheCompanyScopedOverrideUsesTheOneCascade(ScoredDeal):
    """It wrote the score straight onto the row: no ReviewLog, and no
    re-scoring — so the panel showed a corrected row above a headline that
    still reflected the old one."""

    def setUp(self):
        super().setUp()
        self.cbase = f"/api/v1/companies/{self.company.id}/assessment"

    def _override(self, **body):
        return self.client.patch(
            f"{self.cbase}/parameters/TEAM_ADVISORS", body,
            content_type="application/json", **self.headers)

    def test_the_headline_moves_with_the_row(self):
        before = float(self.assessment.overall_score)
        response = self._override(score=9, comment="Roster produced.")
        self.assertEqual(response.status_code, 200)
        self.assessment.refresh_from_db()
        self.assertGreater(float(self.assessment.overall_score), before)

    def test_the_response_carries_the_rescored_summary(self):
        """The panel redraws from this payload, so it has to be the state
        after the write, not before it."""
        before = float(self.assessment.overall_score)
        body = self._override(score=9, comment="Roster produced.").json()
        self.assertGreater(float(body["summary"]["overall_score"]), before)

    def test_the_override_is_written_to_the_review_log(self):
        from fundos.assessment.models import ReviewLog

        self._override(score=9, comment="Roster produced.")
        self.assertTrue(ReviewLog.objects.filter(
            assessment=self.assessment,
            field_or_topic="TEAM_ADVISORS").exists())

    def test_the_machine_s_own_answer_survives(self):
        self._override(score=9, comment="Roster produced.")
        pv = self.assessment.parameter_values.get(input_key="TEAM_ADVISORS")
        self.assertTrue(pv.is_overridden)
        self.assertIsNotNone(pv.system_score)
        self.assertNotEqual(float(pv.score), float(pv.system_score))

    def test_a_band_with_no_score_is_worth_what_the_key_says(self):
        self._override(band="Excellent", comment="Anchor met in full.")
        pv = self.assessment.parameter_values.get(input_key="TEAM_ADVISORS")
        self.assertEqual(float(pv.score), 9.0)

    def test_an_unknown_band_is_refused(self):
        response = self._override(band="Legendary", comment="Because.")
        self.assertEqual(response.status_code, 422)

    def test_a_reason_is_still_required(self):
        response = self._override(score=9)
        self.assertEqual(response.status_code, 422)

    def test_clearing_reverts_and_rescores(self):
        self._override(score=9, comment="Roster produced.")
        after_override = float(
            self.assessment.parameter_values.get(
                input_key="TEAM_ADVISORS").score)
        self._override()
        pv = self.assessment.parameter_values.get(input_key="TEAM_ADVISORS")
        self.assertFalse(pv.is_overridden)
        self.assertEqual(float(pv.score), float(pv.system_score))
        self.assertNotEqual(float(pv.score), after_override)
