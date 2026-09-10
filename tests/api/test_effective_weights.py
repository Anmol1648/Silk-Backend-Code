"""One weight, reported once.

The payload gave two answers to "what share does this row carry": the tree
said a not-evidenced leaf carried 0% and its evidenced sibling 50%, while the
same leaf's `weights` block said 20% either way, because `effectivePct` was
the declared share under another name. `contribution` was derived from the
wrong one, so the parts did not sum to the whole.
"""
from decimal import Decimal

from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base


class EffectiveWeightIsTheAppliedShare(Phase1Base):

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        # Two siblings in A.1 evidenced, the rest left blank, so the blank
        # rows' weight has to redistribute and the difference is visible.
        for key, value in (("TEAM_COFDR_YRS", "25"), ("TEAM_FDR_EXP", "15")):
            ParameterValue.objects.update_or_create(
                assessment=self.assessment, input_key=key,
                defaults={"raw_value": value, "category": "A",
                          "source_type": "document", "source_tier": 1,
                          "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _payload(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        response = self.client.get(url, **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def _leaves(self, data):
        """Every tree leaf paired with the evidence dict it displays."""
        out = []

        def walk(node):
            if "children" not in node and "subitems" not in node:
                out.append((node, node.get("parameter") or node))
            for child in node.get("subitems") or node.get("children") or []:
                walk(child)

        for cat in data["categories"]:
            walk(cat)
        self.flat = {node["input_key"]: node for node, _ in out}
        return out

    def test_the_tree_and_the_flat_row_report_the_same_share(self):
        leaves = self._leaves(self._payload())
        for node, row in leaves:
            flat = self.flat.get(node["input_key"])
            if flat is not None:
                self.assertIs(flat, row, node["input_key"])
            if row["score"] is not None and node["applied_weight"] == 0:
                continue  # an orphan outside the roll-up; see the serializer
            self.assertAlmostEqual(
                row["weight"]["applied"], node["applied_weight"],
                places=3, msg=node["input_key"])

    def test_an_excluded_row_carries_no_effective_weight(self):
        seen = False
        for node, row in self._leaves(self._payload()):
            if not row["weight"]["excluded"]:
                continue
            if row["score"] is not None:
                continue
            seen = True
            self.assertEqual(row["weight"]["applied"], 0.0,
                             node["input_key"])
            self.assertEqual(row["weight"]["effectiveOfTotalPct"], 0.0,
                             node["input_key"])
            self.assertEqual(row["weight"]["contribution"], 0.0, node["input_key"])
        self.assertTrue(seen, "no excluded row in the fixture to check")

    def test_an_evidenced_row_absorbs_its_blank_siblings_weight(self):
        """2.3.1: blank siblings redistribute, they do not score zero."""
        flat = self.flat if hasattr(self, "flat") else {n["input_key"]: n for n, _ in self._leaves(self._payload())}
        row = flat["TEAM_FDR_EXP"]
        self.assertGreater(row["weight"]["applied"],
                           row["weight"]["declared"])

    def test_the_declared_share_is_still_reported_beside_it(self):
        """Redistribution is stated, not hidden: both numbers survive."""
        for _, row in self._leaves(self._payload()):
            self.assertIn("declared", row["weight"])

    def test_the_contributions_sum_to_the_headline(self):
        """The parts add up to the whole, on the same 100-point scale."""
        data = self._payload()
        overall = data["summary"]["overall_score"]
        if overall is None:
            self.skipTest("nothing scored")
        total = sum(row["weight"]["contribution"]
                    for node, row in self._leaves(data)
                    if node.get("feeds_score", True) and row["score"] is not None)
        self.assertAlmostEqual(total, overall * 10, places=1)


class AnchorRowsTraceTheirOwnBasis(Phase1Base):
    """An anchor row is banded from written definitions, not cut-points."""

    def setUp(self):
        super().setUp()
        from fundos.assessment.models import ParameterValue
        from fundos.assessment.services import run_scoring

        # A numeric-looking value on an anchor row: this is what sent the
        # serializer to the rubric engine and back with the wrong reason.
        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="ANC_PRIOR_STARTUP",
            defaults={"raw_value": "1", "category": "A",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

    def _leaves(self, data):
        out = []
        def walk(node):
            if "children" not in node and "subitems" not in node:
                out.append((node, node.get("parameter") or node))
            for child in node.get("subitems") or node.get("children") or []:
                walk(child)
        for cat in data["categories"]:
            walk(cat)
        return out

    def test_an_anchor_row_is_never_told_it_has_no_rubric(self):
        url = f"/api/v1/companies/{self.company.id}/assessment"
        data = self.client.get(url, **self.headers).data
        row = next(n for n, _ in self._leaves(data)
                   if n["input_key"] == "ANC_PRIOR_STARTUP")

        self.assertEqual(row["trace"]["method"], "anchor")
        self.assertNotIn("No rubric is defined", row["trace"]["explanation"])
        self.assertIn("anchor", row["trace"]["explanation"].lower())
