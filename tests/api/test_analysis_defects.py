"""Four defects in the analysis output, each asserted against the real config.

The scorecard's two analysis blocks made four claims that were not true of the
deal they described: contradictions between parameters that cross-check
nothing, a trace calling a scored row excluded, structural market facts ranked
as diligence actions, and one disagreement reported three times.
"""
from decimal import Decimal

from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base


class CrossCheckPairing(Phase1Base):
    """2.4.5 compares two readings of ONE fact, not two rows in one node."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        # A.1.b is declared twice: TEAM_MARQUEE_CLIENTS scores it, and
        # ANC_FDR_NETWORK cross-checks it. They are the pair.
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="TEAM_MARQUEE_CLIENTS",
            # The evidence states the founder link, so the row scores and can
            # be compared against its cross-check. Without the link it would
            # be held unscored and there would be nothing to contradict —
            # which is a different test, in test_decision_output.
            defaults={"raw_value": "6", "band": "Excellent", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9"),
                      "justification": "Six accounts were introduced by the "
                                       "founder from her prior network."})
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="ANC_FDR_NETWORK",
            defaults={"band": "Poor", "category": "A",
                      "is_reference_only": True, "source_type": "document",
                      "source_tier": 1, "confidence": Decimal("0.9")})
        # A.1.a sits in the same sub-item and cross-checks nothing at all.
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="ANC_FDR_EDU",
            defaults={"band": "Excellent", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _contradictions(self):
        return [f for f in self.assessment.audit_findings
                if f.get("check") == "ref_contradiction"]

    def test_the_declared_pair_is_reported(self):
        pairs = [set(f["parameters"]) for f in self._contradictions()]
        self.assertIn({"TEAM_MARQUEE_CLIENTS", "ANC_FDR_NETWORK"}, pairs)

    def test_a_parameter_with_no_declared_partner_is_never_contradicted(self):
        """ANC_FDR_EDU shares a sub-item with the pair and nothing else."""
        named = {k for f in self._contradictions() for k in f["parameters"]}
        self.assertNotIn("ANC_FDR_EDU", named)

    def test_every_contradiction_names_a_real_declared_pair(self):
        """Both keys must resolve to the same ref code, or to its parent node.

        Those are the only two shapes the config uses to declare a pairing: a
        leaf-level ref sharing its partner's ref code, and a node-level ref
        whose code IS the parent it checks.
        """
        from fundos.assessment.models import ConfigParameter

        cfg = {p.input_key: p for p in ConfigParameter.objects.filter(
            is_active=True)}
        for finding in self._contradictions():
            scored_key, ref_key = finding["parameters"]
            scored, refrow = cfg[scored_key], cfg[ref_key]
            self.assertFalse(refrow.feeds_score)
            self.assertTrue(scored.feeds_score)
            node_level = refrow.ref_code == (refrow.parent_code or "")
            self.assertTrue(
                refrow.ref_code == scored.ref_code
                or (node_level
                    and refrow.ref_code == (scored.parent_code
                                            or scored.category_code)),
                f"{scored_key} and {ref_key} are not a declared pair")

    def test_a_contradiction_is_reported_exactly_once(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        findings = self.client.get(url, **self.headers).data[
            "analysis"]["red_flags"]

        # The pair is carried on the two rows it concerns. The audit copy of
        # the same sentence must not also be in the list.
        self.assertFalse([f for f in findings
                          if f.get("check") == "ref_contradiction"])
        seen = [tuple(sorted((f["inputKey"], c)))
                for f in findings for c in f.get("contradictions", [])]
        self.assertEqual(sorted(seen), sorted(set(seen)))


class LookupRowsAreConsistent(Phase1Base):
    """A row is either scored everywhere or excluded everywhere."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        # E.2 is a deal-database percentile: scored here, and holding no
        # rubric in the reference engine that builds every other trace.
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="PCTL_E_2",
            defaults={"raw_value": "9", "category": "E",
                      "source_type": "database", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _payload(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def _leaves(self, data):
        out = []

        def walk(node):
            if "children" not in node and "subitems" not in node:
                out.append(node)
            for child in node.get("subitems") or node.get("children") or []:
                walk(child)

        for cat in data["categories"]:
            walk(cat)
        return out

    def test_a_percentile_row_traces_its_actual_banding(self):
        data = self._payload()
        row = next(p for p in self._leaves(data)
                   if p["input_key"] == "PCTL_E_2")

        self.assertIsNotNone(row["score"])
        self.assertEqual(row["trace"]["method"], "lookup")
        self.assertNotIn("No rubric is defined",
                         row["trace"].get("explanation", ""))
        # The cut-points it was actually banded against.
        self.assertTrue(row["trace"]["thresholds"])
        self.assertEqual(row["trace"]["matched_band"], row["band"])

    def test_no_scored_row_claims_it_was_excluded(self):
        for row in self._leaves(self._payload()):
            if row["score"] is None:
                continue
            self.assertNotEqual(
                row["trace"].get("method"), "excluded",
                f"{row['input_key']} scored {row['score']} but traces as "
                f"excluded")

    def test_trace_weight_and_contribution_agree(self):
        """Excluded in the trace, the weights and the score is ONE fact.

        Asserted in both directions. The mirror of the original defect is
        just as wrong: an unanswered percentile row quoting the cut-point
        table describes an arithmetic that never ran.
        """
        for row in self._leaves(self._payload()):
            excluded_trace = row["trace"].get("method") == "excluded"
            w = row.get("weight") if isinstance(row.get("weight"), dict) else (row.get("weights") or {})
            if excluded_trace:
                self.assertTrue(w.get("excluded"))
                self.assertIsNone(row["score"])
                self.assertEqual(w.get("contribution", 0), 0)
            if w.get("excluded"):
                self.assertIsNone(row["score"])
                # It may carry no trace at all, but it may not carry one
                # claiming a band it never reached.
                self.assertIsNone(row["trace"].get("matched_band"))

    def test_an_excluded_row_is_never_recommended(self):
        data = self._payload()
        excluded = {r["input_key"] for r in self._leaves(data)
                    if (r.get("weight") if isinstance(r.get("weight"), dict) else (r.get("weights") or {})).get("excluded")}
        analysis = data["analysis"]
        offered = {r["inputKey"]
                   for r in analysis["band_recommendations"]["items"]}
        self.assertFalse(excluded & offered)


class RecommendationsAreActionable(Phase1Base):
    """A to-do list may not contain items nobody can do."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="PCTL_E_2",
            defaults={"raw_value": "7", "category": "E",
                      "source_type": "database", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _analysis(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        return self.client.get(url, **self.headers).data["analysis"]

    def test_recommendations_are_all_controllable(self):
        """Controllable by construction now — the block carries only those,
        so the guarantee is that nothing structural got in."""
        items = self._analysis()["band_recommendations"]["items"]
        self.assertTrue(items)
        structural = {"E.2", "E.3", "E.6", "F.2", "F.3", "F.6"}
        self.assertFalse({r["ref"] for r in items} & structural)

    def test_market_and_database_rows_are_separated_not_ranked(self):
        analysis = self._analysis()
        recs = {r["inputKey"]
                for r in analysis["band_recommendations"]["items"]}
        self.assertNotIn("PCTL_E_2", recs,
                         "a deal-database percentile is not a controllable diligence action")

    def test_the_recommendation_block_states_its_own_baseline(self):
        """Every impact is a delta from it, so it travels with the list."""
        analysis = self._analysis()
        block = analysis["band_recommendations"]
        self.assertIn("baseline", block)
        self.assertIsNotNone(block["baseline"]["overall"])
