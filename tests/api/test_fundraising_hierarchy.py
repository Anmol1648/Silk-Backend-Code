"""The Fundraising hierarchy — Parent -> Child -> Grandchild, and A-F only.

The payload used to hand the reader the workbook's own input rows: an anchor
row and its rubric row sat as siblings under A.1 with the same weight counted
twice, the `(ref)` cross-checks appeared as scorecard lines nobody could act
on, the grandchild level the model declares weights for was missing entirely,
and category G — which rates the advisor's fit for the mandate, not the
company — was scored into the company's own rating.

These tests assert the shape a reader is owed: 55 terminal parameters, each
carrying its own value, score, evidence and findings, and every node above one
carrying nothing but a name, a weight and what its children rolled up to.

They run against the REAL seeded model, not a fixture, because a hierarchy
test over a stubbed config proves only that the code reshapes what it was
handed.
"""
from decimal import Decimal

from rest_framework import status

from tests.api.test_fundraising_phase1 import Phase1Base

from fundos.assessment import hierarchy as H

# The contract, restated here by hand rather than imported from the module
# under test — a test that reads its expectations out of the code it is
# checking cannot fail when that code is wrong.
EXPECTED = {
    "A": ("Team", {
        "A.1": ("Founder Profile", [
            ("A.1.a", "Founder Education"),
            ("A.1.b", "Founder Industry Network"),
            ("A.1.c", "Co-Founder Relationship"),
            ("A.1.d", "Founder Industry Experience"),
            ("A.1.e", "Prior Startup Experience")]),
        "A.2": ("Leadership Team", [
            ("A.2.a", "Product & Technology Leadership"),
            ("A.2.b", "Sales & GTM Leadership"),
            ("A.2.c", "Finance Leadership"),
            ("A.2.d", "Operations Leadership")]),
        "A.3": ("Board, Advisors & Investors", [
            ("A.3.a", "Formal Board"),
            ("A.3.b", "Advisor Quality"),
            ("A.3.c", "Investor Quality")]),
    }),
    "B": ("Financials", {
        "B.1": ("Revenue Scale", []), "B.2": ("Revenue Growth", []),
        "B.3": ("Gross Margin CM1", []),
        "B.4": ("Contribution Margin CM2", []),
        "B.5": ("EBITDA Margin", []), "B.6": ("PAT Margin", []),
        "B.7": ("Cash Runway", []), "B.8": ("Working Capital Cycle", []),
        "B.9": ("Debt Level", []),
    }),
    "C": ("Business Quality", {
        "C.1": ("Company Age", []), "C.2": ("Revenue Predictability", []),
        "C.3": ("Competitive Moat", [
            ("C.3.a", "Proprietary IP & Technology"),
            ("C.3.b", "Network Effects"),
            ("C.3.c", "Brand Strength"),
            ("C.3.d", "Customer Stickiness"),
            ("C.3.e", "Scale & Cost Advantage")]),
        "C.4": ("Scalability", []), "C.5": ("Pricing Power", []),
        "C.6": ("Client Concentration", []),
        "C.7": ("Supplier Concentration", []),
        "C.8": ("Government Regulation", []),
    }),
    "D": ("Deal Dynamics", {
        "D.1": ("Raise vs Last Round", []),
        "D.2": ("Raise vs Total Raised", []),
        "D.3": ("Existing Investor Contribution", []),
        "D.4": ("Valuation Expectations", []),
        "D.5": ("Founder Dilution", []),
        "D.6": ("Founder Process Discipline", []),
        "D.7": ("Use of Proceeds", []), "D.8": ("Seller Motivation", []),
    }),
    "E": ("Sector Attractiveness", {
        "E.1": ("Market Size & Growth", []), "E.2": ("Deal Velocity", []),
        "E.3": ("Deal Ticket Size", []), "E.4": ("Peer Tenor", []),
        "E.5": ("Peer Raise Recency", []), "E.6": ("Active Investors", []),
        "E.7": ("News Flow", [("E.7.a", "Peer News Flow"),
                              ("E.7.b", "Sector News Flow")]),
    }),
    "F": ("Sub-Sector Attractiveness", {
        "F.1": ("Market Size & Growth", []), "F.2": ("Deal Velocity", []),
        "F.3": ("Deal Ticket Size", []), "F.4": ("Peer Tenor", []),
        "F.5": ("Peer Raise Recency", []), "F.6": ("Active Investors", []),
    }),
}

EXPECTED_TERMINALS = [
    gc for _cat, (_n, children) in EXPECTED.items()
    for ref, (_cn, gcs) in children.items()
    for gc in ([g[0] for g in gcs] or [ref])
]

# Rows that must never be a node of their own, and must still be configured.
HELPER_KEYS = [
    "TEAM_PRIOR_VENTURES", "TEAM_CXO_SEATS", "FIN_BURN_MULT",
    "FIN_DEBT_EBITDA", "BQ_CHURN", "BQ_PATENTS", "BQ_CAC_TREND",
    "BQ_PRICE_CHG", "BQ_TOP1_CLIENT", "DD_RAISE_TO_ARR",
]

# The advisor-mandate rows. Out of the hierarchy, still in the model.
MANDATE_KEYS = [
    "IB_SECTOR_DEALS", "IB_INVESTOR_RELS", "IB_GTM_WEEKS", "IB_CLOSE_MTHS",
    "IB_LIVE_MANDATES", "IB_DEAL_SIZE", "ANC_IB_SECTOR_KNOW",
    "ANC_IB_CAPACITY",
]


class HierarchyBase(Phase1Base):

    def payload(self):
        response = self.client.get(
            f"/api/v1/companies/{self.company.id}/assessment", **self.headers)
        self.assertEqual(response.status_code, status.HTTP_200_OK)
        return response.data

    def terminals(self, data):
        """{ref: node} for every terminal, and it must be unique by ref."""
        found = {}
        duplicates = []

        def walk(node):
            if node.get("isTerminal"):
                if node["ref"] in found:
                    duplicates.append(node["ref"])
                found[node["ref"]] = node
                return
            for child in (node.get("subitems") or node.get("children")
                          or []):
                walk(child)

        for cat in data["categories"]:
            walk(cat)
        self.assertEqual(duplicates, [], "a terminal ref appeared twice")
        return found

    def all_nodes(self, data):
        out = []

        def walk(node):
            out.append(node)
            for child in (node.get("subitems") or node.get("children")
                          or []):
                walk(child)

        for cat in data["categories"]:
            walk(cat)
        return out


class TheHierarchyIsExactlyAToF(HierarchyBase):

    def test_six_categories_in_order_with_the_contract_names(self):
        data = self.payload()
        self.assertEqual([c["code"] for c in data["categories"]],
                         ["A", "B", "C", "D", "E", "F"])
        self.assertEqual([c["name"] for c in data["categories"]],
                         [EXPECTED[c][0] for c in "ABCDEF"])

    def test_every_child_and_grandchild_carries_the_contract_name(self):
        data = self.payload()
        by_ref = {n["ref"]: n for n in self.all_nodes(data) if "ref" in n}
        for cat, (_cat_name, children) in EXPECTED.items():
            for child_ref, (child_name, gcs) in children.items():
                self.assertIn(child_ref, by_ref, f"{child_ref} missing")
                self.assertEqual(by_ref[child_ref]["name"], child_name)
                for gc_ref, gc_name in gcs:
                    self.assertIn(gc_ref, by_ref, f"{gc_ref} missing")
                    self.assertEqual(by_ref[gc_ref]["name"], gc_name)

    def test_a_category_nothing_scored_is_still_one_of_the_six(self):
        """A-F is a contract, not a report of what happened to be scored."""
        from fundos.assessment.models import CategoryScore
        CategoryScore.objects.filter(assessment=self.assessment,
                                     category_code="F").delete()
        data = self.payload()
        self.assertEqual([c["code"] for c in data["categories"]],
                         ["A", "B", "C", "D", "E", "F"])
        f = next(c for c in data["categories"] if c["code"] == "F")
        self.assertEqual(f["name"], "Sub-Sector Attractiveness")
        self.assertIsNone(f["score"])
        self.assertEqual(len(f["subitems"]), 6)
        self.assertEqual(len(self.terminals(data)), 55)

    def test_the_tree_is_never_deeper_than_three_levels(self):
        data = self.payload()

        def depth(node, level=1):
            kids = node.get("subitems") or node.get("children") or []
            return max([depth(k, level + 1) for k in kids] or [level])

        for cat in data["categories"]:
            self.assertLessEqual(depth(cat), 3, f"{cat['code']} nests deeper")

    def test_all_55_terminals_exist_exactly_once_under_the_right_parent(self):
        data = self.payload()
        found = self.terminals(data)
        self.assertEqual(sorted(found), sorted(EXPECTED_TERMINALS))
        self.assertEqual(len(found), 55)

        # And each one hangs where the contract says, at the right level.
        for cat in data["categories"]:
            for child in cat["subitems"]:
                if child.get("isTerminal"):
                    self.assertEqual(child["level"], "child")
                    self.assertEqual(child["ref"].split(".")[0], cat["code"])
                    continue
                for gc in child["children"]:
                    self.assertEqual(gc["level"], "grandchild")
                    self.assertTrue(gc["ref"].startswith(child["ref"] + "."))


class TerminalsCarryTheAssessment(HierarchyBase):

    def test_a_terminal_never_declares_an_empty_child_list(self):
        for node in self.terminals(self.payload()).values():
            self.assertNotIn("children", node, node["ref"])
            self.assertNotIn("subitems", node, node["ref"])
            self.assertNotIn("parameters", node, node["ref"])

    def test_every_terminal_carries_the_full_parameter_record(self):
        for ref, node in self.terminals(self.payload()).items():
            for field in ("value", "score", "band", "weight", "evidence",
                          "trace", "rubric", "anchor", "key",
                          "diligenceFindings", "status"):
                self.assertIn(field, node, f"{ref} is missing {field}")
            # `inputs` appears ONLY where more than one row feeds the
            # terminal. A single-input terminal IS that row, and echoing it
            # was a fifth of the payload saying nothing.
            if "inputs" in node:
                self.assertGreater(len(node["inputs"]), 1, ref)
            for field in ("tier", "confidence", "reasoning", "citations"):
                self.assertIn(field, node["evidence"], f"{ref}.{field}")
            self.assertTrue(node["isTerminal"])
            self.assertFalse(node.get("is_reference"))

    def test_an_answered_terminal_keeps_its_evidence_and_citations(self):
        # TEAM_FDR_EXP was answered from the deck at tier 1 in the fixture.
        node = self.terminals(self.payload())["A.1.d"]
        self.assertIsNotNone(node["score"])
        self.assertEqual(node["value"]["raw"], 12.0)
        self.assertEqual(node["band"], "Excellent")
        self.assertEqual(node["evidence"]["tier"], "Verified")
        self.assertEqual(node["evidence"]["confidence"], "high")
        self.assertIn("TEAM_FDR_EXP", node["evidence"]["reasoning"])
        self.assertTrue(node["evidence"]["citations"])
        self.assertTrue(node["trace"])

    def test_a_terminal_shows_the_rubric_and_the_anchor_behind_it(self):
        """Both, wherever either input holds one — not just the primary's."""
        found = self.terminals(self.payload())
        self.assertIsNotNone(found["A.3.b"]["rubric"],
                             "A.3.b lost TEAM_ADVISORS' rubric")
        self.assertIsNotNone(found["A.3.b"]["anchor"],
                             "A.3.b lost ANC_ADVISORS' anchor definitions")
        self.assertIsNotNone(found["A.1.d"]["rubric"])
        self.assertIsNotNone(found["A.1.a"]["anchor"])

    def test_a_terminal_nobody_answered_is_present_and_unscored(self):
        node = self.terminals(self.payload())["B.5"]
        self.assertTrue(node["isTerminal"])
        self.assertIsNone(node["score"])
        self.assertEqual(node["status"], "not_evidenced")
        self.assertTrue(node["weight"]["excluded"])

    def test_findings_are_attached_to_the_terminal_they_are_about(self):
        # TEAM_ADVISORS was answered at tier 3 / 0.30 confidence, which is
        # exactly what raises a diligence finding.
        data = self.payload()
        node = self.terminals(data)["A.3.b"]
        self.assertTrue(node["diligenceFindings"],
                        "A.3.b should carry its own low-confidence finding")
        self.assertTrue(all(f.get("inputKey") in ("TEAM_ADVISORS",
                                                  "ANC_ADVISORS")
                            for f in node["diligenceFindings"]))


class GroupingNodesCarryNothingElse(HierarchyBase):

    def test_a_parent_or_child_holds_only_grouping_fields(self):
        data = self.payload()
        allowed = {"ref", "code", "name", "level", "isTerminal", "weight",
                   "applied_weight", "score", "band", "description",
                   "subitems", "children"}
        for node in self.all_nodes(data):
            if node.get("isTerminal"):
                continue
            self.assertFalse(set(node) - allowed,
                             f"{node.get('ref') or node.get('code')} carries "
                             f"{sorted(set(node) - allowed)}")
            for banned in ("evidence", "citations", "value", "trace",
                           "rubric", "anchor", "inputs"):
                self.assertNotIn(banned, node)

    def test_a_non_terminal_declares_its_children(self):
        data = self.payload()
        for cat in data["categories"]:
            self.assertFalse(cat["isTerminal"])
            self.assertTrue(cat["subitems"])
            for child in cat["subitems"]:
                if child["isTerminal"]:
                    continue
                self.assertTrue(child["children"])
                self.assertTrue(all(p["isTerminal"]
                                    for p in child["children"]))

    def test_the_word_parameters_appears_nowhere_in_the_response(self):
        """Below a category the key is `children`, everywhere, always."""
        def walk(node, path="$"):
            hits = []
            if isinstance(node, dict):
                for key, value in node.items():
                    if key == "parameters":
                        hits.append(f"{path}.{key}")
                    hits += walk(value, f"{path}.{key}")
            elif isinstance(node, list):
                for i, value in enumerate(node):
                    hits += walk(value, f"{path}[{i}]")
            return hits

        self.assertEqual(walk(self.payload()), [])


class InputsAreCarriedNotExposed(HierarchyBase):

    def test_no_helper_row_is_ever_a_node(self):
        data = self.payload()
        node_keys = {n.get("key") for n in self.all_nodes(data)}
        node_refs = {n.get("ref") for n in self.all_nodes(data)}
        for key in HELPER_KEYS:
            self.assertNotIn(key, node_refs, f"{key} surfaced as a node ref")
        # A helper may be the row a terminal's evidence came from only when
        # nothing else answered it; it is never its own line on the card.
        self.assertNotIn("TEAM_CXO_SEATS", node_keys)

    def test_helper_rows_stay_configured_and_readable(self):
        from fundos.assessment.models import ConfigParameter
        for key in HELPER_KEYS:
            cfg = ConfigParameter.objects.filter(
                input_key=key, tenant_id__isnull=True, is_active=True).first()
            self.assertIsNotNone(cfg, f"{key} was removed from the config")
            self.assertFalse(cfg.feeds_score)

    def test_helper_rows_are_reachable_inside_their_terminal(self):
        found = self.terminals(self.payload())
        for ref, key in (("A.1.e", "TEAM_PRIOR_VENTURES"),
                         ("B.7", "FIN_BURN_MULT"),
                         ("B.9", "FIN_DEBT_EBITDA"),
                         ("C.2", "BQ_CHURN"),
                         ("C.5", "BQ_PRICE_CHG"),
                         ("C.6", "BQ_TOP1_CLIENT"),
                         ("D.2", "DD_RAISE_TO_ARR")):
            keys = {i["key"]: i for i in found[ref]["inputs"]}
            self.assertIn(key, keys, f"{key} lost from {ref}")
            self.assertTrue(keys[key]["is_reference"])

    def test_multi_input_terminals_merge_every_row_that_feeds_them(self):
        found = self.terminals(self.payload())
        for ref, keys in (("A.1.b", {"TEAM_MARQUEE_CLIENTS",
                                     "ANC_FDR_NETWORK"}),
                          ("A.3.b", {"TEAM_ADVISORS", "ANC_ADVISORS"}),
                          ("A.3.c", {"TEAM_INST_INV", "ANC_INVESTOR_QUALITY"}),
                          ("C.3.c", {"BQ_ORGANIC_PCT", "ANC_MOAT_BRAND"}),
                          ("C.5", {"BQ_GM_TREND", "BQ_PRICE_CHG",
                                   "ANC_PRICING_POWER"}),
                          ("D.4", {"DD_VAL_PREMIUM", "ANC_VAL_FLEX"}),
                          ("D.6", {"DD_DIRECT_APPROACHES", "ANC_FDR_BANKER"}),
                          ("E.1", {"SEC_TAM", "SEC_TAM_CAGR"}),
                          ("F.1", {"SEC_TAM_SUB", "SEC_TAM_CAGR_SUB"}),
                          ("E.7.a", {"SEC_PEER_NEWS_CNT", "ANC_PEER_NEWS"}),
                          ("E.7.b", {"SEC_SECTOR_SENTIMENT",
                                     "ANC_SECTOR_NEWS"})):
            self.assertEqual({i["key"] for i in found[ref]["inputs"]}, keys,
                             f"{ref} does not merge the rows that feed it")

    def test_the_percentile_terminals_are_the_lookup_rows_themselves(self):
        found = self.terminals(self.payload())
        for ref, key in (("E.2", "PCTL_E_2"), ("E.3", "PCTL_E_3"),
                         ("E.6", "PCTL_E_6"), ("F.2", "PCTL_F_2"),
                         ("F.3", "PCTL_F_3"), ("F.6", "PCTL_F_6")):
            # One row answers each, so the terminal IS that row and carries
            # no `inputs` echo of itself.
            self.assertEqual(found[ref]["key"], key)
            self.assertNotIn("inputs", found[ref])
            self.assertFalse(found[ref]["is_reference"])


class MandateContextIsGoneFromFundraising(HierarchyBase):

    def test_no_g_anywhere_in_the_payload(self):
        data = self.payload()
        self.assertNotIn("G", [c["code"] for c in data["categories"]])
        for node in self.all_nodes(data):
            ref = str(node.get("ref") or node.get("code") or "")
            self.assertFalse(ref == "G" or ref.startswith("G."),
                             f"{ref} is in the Fundraising tree")

    def test_no_mandate_row_reaches_the_tree_or_the_findings(self):
        import json
        data = self.payload()
        blob = json.dumps(data, default=str)
        for key in MANDATE_KEYS:
            self.assertNotIn(key, blob, f"{key} reached the payload")
        self.assertNotIn("Mandate Context", blob)

    def test_the_mandate_config_and_its_rubrics_are_untouched(self):
        from fundos.assessment.models import (ConfigParameter, ConfigRubric,
                                              ConfigWeight)
        for key in MANDATE_KEYS:
            self.assertTrue(
                ConfigParameter.objects.filter(
                    input_key=key, tenant_id__isnull=True,
                    is_active=True).exists(),
                f"{key} was removed from the config")
        self.assertTrue(ConfigWeight.objects.filter(
            level="category", code="G", is_active=True).exists())
        self.assertEqual(ConfigWeight.objects.filter(
            level="subitem", parent_code="G", is_active=True).count(), 5)
        self.assertTrue(ConfigRubric.objects.filter(
            input_key="IB_GTM_WEEKS", is_active=True).exists())

    def test_the_mandate_rows_are_still_scored_by_the_engine(self):
        """G still rolls up in `run_scoring` — it is a read-path exclusion."""
        from fundos.assessment.models import CategoryScore, ParameterValue
        from fundos.assessment.services import run_scoring

        ParameterValue.objects.update_or_create(
            assessment=self.assessment, input_key="IB_GTM_WEEKS",
            defaults={"raw_value": "4", "category": "G",
                      "source_type": "document", "source_tier": 1,
                      "confidence": Decimal("0.9")})
        run_scoring(self.assessment)
        g = CategoryScore.objects.filter(assessment=self.assessment,
                                         category_code="G").first()
        self.assertIsNotNone(g, "G stopped being scored")
        self.assertIsNotNone(g.score)
        # ...and still never reaches the Fundraising reader.
        self.assertNotIn("G", [c["code"] for c in self.payload()["categories"]])

    def test_the_seeded_category_weights_were_not_rewritten(self):
        """A-F declare 90; `weighted_rollup` renormalises, config does not."""
        from fundos.assessment.models import ConfigWeight
        total = sum(float(w.weight) for w in ConfigWeight.objects.filter(
            level="category", is_active=True))
        self.assertEqual(total, 100.0)
        af = sum(float(w.weight) for w in ConfigWeight.objects.filter(
            level="category", is_active=True).exclude(code="G"))
        self.assertEqual(af, 90.0)

        data = self.payload()
        self.assertEqual(sum(c["weight"] for c in data["categories"]), 90.0)
        scored = [c for c in data["categories"] if c["score"] is not None]
        self.assertAlmostEqual(
            sum(c["applied_weight"] for c in scored), 100.0, places=2)


class ScoresSurviveTheRegrouping(HierarchyBase):

    def test_a_terminal_reports_the_score_its_input_was_given(self):
        from fundos.assessment.models import ParameterValue
        found = self.terminals(self.payload())
        for ref, key in (("A.1.c", "TEAM_COFDR_YRS"),
                         ("A.1.d", "TEAM_FDR_EXP"),
                         ("A.3.c", "TEAM_INST_INV")):
            pv = ParameterValue.objects.get(assessment=self.assessment,
                                            input_key=key)
            self.assertEqual(found[ref]["score"], float(pv.score))
            self.assertEqual(found[ref]["band"], pv.band)

    def test_a_child_is_the_weighted_roll_up_of_its_grandchildren(self):
        from fundos.engines.deal_assessment import weighted_rollup
        data = self.payload()
        for cat in data["categories"]:
            for child in cat["subitems"]:
                if child["isTerminal"]:
                    continue
                expected, _ = weighted_rollup([
                    {"code": p["ref"], "score": p["score"],
                     "weight": p["weight"]["declared"]}
                    for p in child["children"]])
                if expected is None:
                    self.assertIsNone(child["score"])
                else:
                    self.assertAlmostEqual(child["score"],
                                           round(float(expected), 2), places=2)

    def test_the_overall_is_the_roll_up_of_a_to_f(self):
        from fundos.engines.deal_assessment import weighted_rollup
        data = self.payload()
        expected, _ = weighted_rollup([
            {"code": c["code"], "score": c["score"], "weight": c["weight"]}
            for c in data["categories"]])
        self.assertAlmostEqual(data["summary"]["overall_score"],
                               round(float(expected), 2), places=2)

    def test_the_parts_add_up_to_the_whole(self):
        """Every terminal's contribution sums back to the headline score.

        This is the check that catches G being half-removed: an overall
        rolled over A-F while the shares underneath were still normalised
        over all seven leaves the parts summing to 90% of the whole.
        """
        data = self.payload()
        contributions = sum(
            (n["weight"].get("contribution") or 0.0)
            for n in self.terminals(data).values())
        self.assertAlmostEqual(contributions / 10.0,
                               data["summary"]["overall_score"], places=2)

    def test_partial_data_leaves_the_shape_intact(self):
        """Four answers out of eighty: still 55 terminals, still six parents."""
        data = self.payload()
        found = self.terminals(data)
        self.assertEqual(len(found), 55)
        scored = [n for n in found.values() if n["score"] is not None]
        self.assertTrue(0 < len(scored) < 55)
        self.assertLess(data["summary"]["coverage"]["coverage_pct"], 100.0)

    def test_complete_data_scores_every_terminal(self):
        from fundos.assessment.models import ConfigParameter, ParameterValue
        from fundos.assessment.services import run_scoring

        # Answer every scoring row in A-F. An anchor row arrives already
        # banded from upstream (that is what `band_parameter` guards), a
        # rubric row takes a number, a percentile row a 0-10 score. The
        # justification carries the attribution wording the three
        # ATTRIBUTION_CLAIMS rows are held back without.
        justification = ("Founder relationship: introduced through the "
                         "founder's own network, quarterly cadence, board "
                         "minutes and packs on file.")
        for cfg in ConfigParameter.objects.filter(
                tenant_id__isnull=True, is_active=True, feeds_score=True):
            if not H.is_fundraising_category(cfg.category_code):
                continue
            anchor = cfg.scoring_type == "anchor"
            raw = ("Good" if anchor
                   else "7" if cfg.scoring_type == "lookup" else "50")
            ParameterValue.objects.update_or_create(
                assessment=self.assessment, input_key=cfg.input_key,
                defaults={"raw_value": raw, "category": cfg.category_code,
                          "band": "Good" if anchor else "",
                          "source_type": "document", "source_tier": 1,
                          "source_detail": "model.xlsx, Inputs",
                          "confidence": Decimal("0.9"),
                          "justification": justification})
        run_scoring(self.assessment)
        self.assessment.refresh_from_db()

        data = self.payload()
        found = self.terminals(data)
        self.assertEqual(len(found), 55)
        unscored = [r for r, n in found.items() if n["score"] is None]
        self.assertEqual(unscored, [], f"unscored with full data: {unscored}")
        self.assertIsNotNone(data["summary"]["overall_score"])
        self.assertEqual(data["summary"]["coverage"]["coverage_pct"], 100.0)
        for cat in data["categories"]:
            self.assertIsNotNone(cat["score"], cat["code"])


class TheModuleAndThePayloadAgree(HierarchyBase):
    """The hierarchy module is the contract; nothing else may restate it."""

    def test_the_module_declares_exactly_55_terminals(self):
        self.assertEqual(len(H.TERMINAL_REFS), 55)
        self.assertEqual(sorted(H.TERMINAL_REFS), sorted(EXPECTED_TERMINALS))

    def test_g_is_not_a_fundraising_category(self):
        for code in "ABCDEF":
            self.assertTrue(H.is_fundraising_category(code))
        self.assertFalse(H.is_fundraising_category("G"))
        self.assertFalse(H.is_fundraising_category("G.1"))

    def test_a_non_terminal_ref_resolves_to_no_terminal(self):
        self.assertEqual(H.terminal_ref_for("A.1"), "")
        self.assertEqual(H.terminal_ref_for("A.2"), "")
        self.assertEqual(H.terminal_ref_for("C.3"), "")
        self.assertEqual(H.terminal_ref_for("E.7"), "")
        self.assertEqual(H.terminal_ref_for("G.1"), "")
        self.assertEqual(H.terminal_ref_for("B.1"), "B.1")
        self.assertEqual(H.terminal_ref_for("A.1.a"), "A.1.a")
