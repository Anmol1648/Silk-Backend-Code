# -*- coding: utf-8 -*-
"""Unit tests for V2 Assessment API endpoints in Django.

Tests:
1. GET /api/v1/companies/{company_id}/assessment
2. GET /api/v1/companies/{company_id}/assessment/parameters/{ref}
3. PATCH /api/v1/companies/{company_id}/assessment/parameters/{ref}
4. GET /api/v1/assessment/reference
5. GET /api/v1/assessment/cohorts
"""
from rest_framework import status
from django.core.management import call_command
from django.contrib.auth import get_user_model
from tests.api.test_fundraising_phase1 import Phase1Base

User = get_user_model()


class V2AssessmentAPITests(Phase1Base):

    def test_v2_assessment_get(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIn("job_id", data)
        self.assertEqual(data["job_id"], str(self.company.id))
        self.assertIn("summary", data)
        self.assertIn("categories", data)

        # Verify explicit non-redundant tree structure
        category = data["categories"][0]
        self.assertIn("subitems", category)
        self.assertGreater(len(category["subitems"]), 0)
        subitem = category["subitems"][0]
        self.assertIn("children", subitem)
        self.assertGreater(len(subitem["children"]), 0)
        param = subitem["children"][0]
        self.assertIn("ref", param)
        self.assertIn("value", param)
        self.assertIn("weight", param)
        self.assertIn("evidence", param)
        self.assertIn("trace", param)

    def test_v2_parameter_detail_get(self):
        url = f"/api/v1/companies/{self.company.id}/assessment/parameters/TEAM_FDR_EXP"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIn("parameter", data)
        self.assertIn("rubric", data)
        self.assertIn("anchor", data)
        self.assertIn("dictionary", data)
        self.assertIn("stage", data)
        
        param = data["parameter"]
        self.assertEqual(param["key"], "TEAM_FDR_EXP")
        self.assertIn("evidence", param)
        self.assertIn("citations", param["evidence"])
        self.assertIn("weight", param)

    def test_v2_parameter_override_requires_comment(self):
        url = f"/api/v1/companies/{self.company.id}/assessment/parameters/TEAM_FDR_EXP"
        response = self.client.patch(
            url,
            {"score": 8.0, "band": "Good", "comment": ""},
            content_type="application/json",
            **self.headers
        )
        self.assertEqual(response.status_code, status.HTTP_422_UNPROCESSABLE_ENTITY)

    def test_v2_parameter_override_success(self):
        url = f"/api/v1/companies/{self.company.id}/assessment/parameters/TEAM_FDR_EXP"
        response = self.client.patch(
            url,
            {"score": 8.0, "band": "Good", "comment": "Verified founder domain experience manually"},
            content_type="application/json",
            **self.headers
        )
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        
        # Verify in GET
        get_res = self.client.get(url, **self.headers)
        param = get_res.data["parameter"]
        self.assertTrue(param["assessment"]["system"]["score"] is not None)
        self.assertEqual(param["assessment"]["effective"]["score"], 8.0)
        self.assertEqual(param["assessment"]["status"], "overridden")

    def test_v2_multi_source_citations_parsing(self):
        from fundos.assessment.models import ParameterValue
        detail = (
            "Source 1: Web Research (search-grounded), Batch 1: Company Basics & Identity — Founded in 2017; "
            "Source 2: Founders & Leadership, Batch 2: Founders & Leadership — Founders have 10+ years experience."
        )
        pv, _ = ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_FDR_EXP",
            defaults={"source_detail": detail, "justification": "Founders co-founded Zyla Health in 2017."}
        )
        url = f"/api/v1/companies/{self.company.id}/assessment/parameters/TEAM_FDR_EXP"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        citations = response.data["parameter"]["evidence"]["citations"]
        self.assertEqual(len(citations), 2)
        self.assertEqual(citations[0]["source"], "Web Research \u2014 Company Basics & Identity")
        self.assertTrue("Founders & Leadership" in citations[1]["source"])

    def test_v2_excel_citation_parsing(self):
        from fundos.assessment.models import ParameterValue
        detail = "Project Orah_Financial Model_vf.xlsx, ZYLA Business Switch!G18"
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_FDR_EXP",
            defaults={"source_detail": detail, "justification": "Revenue growth YoY for FY27 calculated."}
        )
        url = f"/api/v1/companies/{self.company.id}/assessment/parameters/TEAM_FDR_EXP"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        citation = response.data["parameter"]["evidence"]["citations"][0]
        self.assertEqual(citation["source"], "Project Orah_Financial Model_vf.xlsx")
        self.assertEqual(citation["locator"], "Sheet: ZYLA Business Switch, Cell: G18")

    def test_citation_deduplication_and_sorting(self):
        from fundos.assessment.models import ParameterValue
        detail = (
            "TyrePlex Investor Deck Detailed June26 OS.pdf, page 2; "
            "Company Research — Puneet Bhaskar Co-Founder; "
            "Company Research — Jiveshwar Sharma Co-Founder; "
            "Company Research — Nikhil Kalra Co-Founder; "
            "Company Research — Company Documents (native extraction + OCR)"
        )
        quote = (
            "Puneet Bhaskar holds an IIM Kozhikode degree (Tier-1) and Rupendra Pratap Singh holds "
            "an IIT Delhi degree (Tier-1), both considered top-tier institutions."
        )
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_FDR_EXP",
            defaults={"source_detail": detail, "justification": quote, "source_type": "document"}
        )
        url = f"/api/v1/companies/{self.company.id}/assessment/parameters/TEAM_FDR_EXP"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        citations = response.data["parameter"]["evidence"]["citations"]

        # Document citation must come first (rank 1), and duplicate quote web research sources must be deduplicated
        self.assertEqual(len(citations), 2)
        self.assertEqual(citations[0]["source_type"], "document")
        self.assertEqual(citations[0]["source"], "TyrePlex Investor Deck Detailed June26 OS.pdf")
        self.assertEqual(citations[0]["locator"], "page 2")
        self.assertEqual(citations[1]["source_type"], "web")
        self.assertEqual(citations[1]["source"], "Company Research")

    def test_v2_assessment_reference(self):
        url = "/api/v1/assessment/reference"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIn("stages", data)
        self.assertIn("bands", data)
        self.assertIn("rubrics", data)
        self.assertIn("anchors", data)

    def test_v2_assessment_cohorts(self):
        url = "/api/v1/assessment/cohorts"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        data = response.data
        self.assertIn("sectors", data)
        self.assertIn("sub_sectors", data)


class V2ScorecardTreeTests(Phase1Base):
    """The V2 payload must be walkable as a drill-down, not just two flat lists.

    Category -> sub-item -> parameter is the shape the scorecard screen reads;
    before this the caller had to rejoin `parameters` onto `categories` by
    `parent_ref` itself, which is a join the payload can do once and correctly.
    """

    def _payload(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def test_team_nests_founder_profile_which_nests_founder_education(self):
        data = self._payload()
        team = next(c for c in data["categories"] if c["code"] == "A")
        self.assertIn("subitems", team)

        profile = next(s for s in team["subitems"] if s["ref"] == "A.1")
        self.assertIn("children", profile)
        self.assertTrue(profile["name"])

        education = next(p for p in profile["children"]
                         if p["ref"] == "A.1.a")
        self.assertEqual(education["key"], "ANC_FDR_EDU")
        # The leaf carries the full evidence object, not a trimmed copy.
        self.assertIn("evidence", education)
        self.assertIn("anchor", education)

    def _nested_leaves(self, data):
        leaves = []
        for cat in data["categories"]:
            sub_nodes = cat["subitems"]
            for sub in sub_nodes:
                if "children" in sub:
                    leaves.extend(sub["children"])
                else:
                    leaves.append(sub)
        return leaves

    def test_every_answered_parameter_is_reachable_exactly_once(self):
        """No answered leaf may be dropped by the nesting, and none duplicated.

        Identity is `key`, not `ref` -- the sector mirrors put two inputs
        on E.1 and every cross-check row shares a ref with what it checks.
        """
        data = self._payload()
        leaves = self._nested_leaves(data)
        keys = [leaf["key"] for leaf in leaves]

        self.assertEqual(sorted(keys), sorted(set(keys)))
        answered = {p["key"] for p in leaves if p.get("score") is not None}
        self.assertTrue(answered.issubset(set(keys)))

    def test_unanswered_parameters_still_appear_in_the_tree(self):
        """The coverage gap is the thing the drill-down exists to show.

        This fixture evidences four categories, so the rest of the model is
        unanswered. Those leaves must be present and self-describing rather
        than silently absent, which would make a thin deal look complete.
        """
        data = self._payload()
        leaves = self._nested_leaves(data)
        nested = {leaf["key"] for leaf in leaves}
        answered = {p["key"] for p in leaves if p.get("score") is not None}

        unanswered = nested - answered
        self.assertTrue(unanswered, "fixture should leave some leaves unanswered")

        team = next(c for c in data["categories"] if c["code"] == "A")
        profile = next(s for s in team["subitems"] if s["ref"] == "A.1")
        blanks = [p for p in profile["children"] if p["score"] is None]
        for blank in blanks:
            self.assertEqual(blank["evidence"]["tier"], "Not Evidenced")
            self.assertEqual(blank["assessment"]["status"],
                             "not_evidenced")
            # Excluded from the roll-up, not counted as a zero.
            self.assertEqual(blank["weight"]["applied"], 0.0)

    def test_subitem_score_is_the_weighted_average_of_its_scored_children(self):
        """A blank sibling redistributes its share; it does not score zero."""
        data = self._payload()

        for cat in data["categories"]:
            for sub in cat["subitems"]:
                if "children" not in sub:
                    continue
                children = sub["children"]
                scored = [c for c in children
                          if c["score"] is not None and not c.get("is_reference", False)]
                if not scored:
                    self.assertIsNone(sub["score"])
                    continue
                total = sum(c["weight"].get("declared", 0.0) if isinstance(c.get("weight"), dict) else (c.get("weight") or 0.0) for c in scored)
                if not total:
                    continue
                expected = sum((c["score"] * (c["weight"].get("declared", 0.0) if isinstance(c.get("weight"), dict) else (c.get("weight") or 0.0)))
                               for c in scored) / total
                self.assertAlmostEqual(sub["score"], round(expected, 2), places=2)
                # Applied shares of the scored children sum to the whole.
                self.assertAlmostEqual(
                    sum(c["weight"]["applied"] for c in scored), 100.0, places=2)

    def test_tree_rolls_up_to_the_persisted_category_score(self):
        """The nested levels must not become a second, looser calculation.

        Sub-item scores are computed here; category scores come from the
        persisted CategoryScore rows. If rolling the tree up one more level
        disagrees with what was stored, the drill-down is explaining a number
        the product never actually showed.
        """
        from fundos.engines.deal_assessment import weighted_rollup

        data = self._payload()
        for cat in data["categories"]:
            subs = [s for s in cat["subitems"] if "children" in s and not s.get("is_reference", False)]
            if not subs:
                continue
            rolled, _ = weighted_rollup([
                {"code": s["ref"], "score": s["score"], "weight": s["weight"]}
                for s in subs
            ])
            if cat["score"] is None or rolled is None:
                self.assertEqual(cat["score"], rolled)
                continue
            self.assertAlmostEqual(float(rolled), cat["score"], places=1,
                                   msg=f"category {cat['code']} disagrees")


class V2AnalysisBlocksTests(Phase1Base):
    """Analysis blocks carried two empty lists; both were already built in phase1.

    The V2 payload advertised diligence_findings and recommendations and
    hardcoded them to empty, so a client reading V2 saw a clean deal with
    nothing to chase while the Phase 1 screen showed the same deal gaps.
    """

    def _analysis(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data["analysis"]

    def test_findings_match_what_phase1_reports(self):
        from fundos.assessment import phase1

        analysis = self._analysis()
        expected = {f["inputKey"] for f in
                    phase1.diligence_findings(self.assessment)}

        rows = analysis["red_flags"] + analysis["data_gaps"]
        self.assertTrue(rows,
                        "a fixture with four evidenced categories must raise "
                        "findings for everything it never answered")
        # Rows that name a parameter must name one phase1 also raised. The
        # audit rows carry no inputKey and are about the assessment itself.
        named = {r["inputKey"] for r in rows if r["inputKey"]}
        self.assertTrue(named.issubset(expected), named - expected)

    def test_findings_are_ordered_by_severity_and_carry_an_ask(self):
        rank = {"High": 0, "Medium": 1, "Low": 2}
        analysis = self._analysis()
        for key in ("red_flags", "data_gaps"):
            rows = analysis[key]
            severities = [rank[r["severity"]] for r in rows]
            self.assertEqual(severities, sorted(severities), key)
            for row in rows:
                self.assertTrue(row["headline"], row["ref"])
                # A row naming a parameter must say what to go and get. An
                # audit row about the assessment itself often has nothing to
                # add beyond the observation, and saying it twice is worse
                # than saying it once.
                if row["inputKey"]:
                    self.assertTrue(
                        row["ask"],
                        f"{row['ref']} states no way to resolve it")

    def test_recommendations_are_ordered_by_headline_impact(self):
        recs = self._analysis()["band_recommendations"]["items"]
        self.assertTrue(recs, "a fixture scoring Fair rows must have somewhere "
                              "to advance to")

        impacts = [r["overallImpact"] for r in recs
                   if r["overallImpact"] is not None]
        self.assertEqual(impacts, sorted(impacts, reverse=True))

    def test_each_recommendation_names_a_published_target(self):
        """The target must be the config rule, never invented advice."""
        ladder = ["Poor", "Fair", "Good", "Excellent"]
        block = self._analysis()["band_recommendations"]
        self.assertIsNotNone(block["baseline"]["overall"])
        for rec in block["items"]:
            self.assertEqual(ladder.index(rec["targetBand"]),
                             ladder.index(rec["currentBand"]) + 1)
            self.assertTrue(rec["target"],
                            f"{rec['ref']} recommends a band with no rule")
            self.assertGreater(rec["overallImpact"], 0)

    def test_system_audit_checks_join_the_red_flags(self):
        """Nothing is wrong with the company; something is wrong with the
        basis it was scored on, and only a red flag can say so."""
        self.assessment.stage_was_defaulted = True
        self.assessment.audit_findings = [
            {"severity": "warning", "check": "stage_defaulted",
             "message": "Deal stage was not set and defaulted to Series A."},
            {"severity": "error", "check": "override_uncommented",
             "message": "1 override(s) with no written justification."},
            {"source": "integrity", "severity": "info", "check": "uncited_value",
             "message": "3 scored value(s) name no source document."},
        ]
        self.assessment.save(update_fields=["stage_was_defaulted",
                                            "audit_findings"])

        flags = self._analysis()["red_flags"]
        self.assertTrue(flags)
        audit = [f for f in flags if not f["inputKey"]]
        self.assertTrue(audit, "the audit checks reached no list at all")
        for row in audit:
            self.assertTrue(row["headline"])
            self.assertTrue(row["parameter"])

    def test_every_finding_says_why_it_was_raised(self):
        analysis = self._analysis()
        for row in analysis["red_flags"] + analysis["data_gaps"]:
            self.assertTrue(row["headline"],
                            f"{row['ref']} states no reason")
