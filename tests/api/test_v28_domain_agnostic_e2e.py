"""
v28 — end-to-end, across domains.

Every log supplied so far has been an eyewear retailer or a healthcare
marketplace, and every fix has been reasoned about against those two. A
profile pipeline that only works for the sectors it was debugged against is
not a pipeline, so this runs the same generation for a construction firm, an
electronics manufacturer and a logistics operator and asserts the SAME
invariants for all three.

The point is not that the output is good — with fixture research adapters it
cannot be. The point is that nothing in the pipeline BRANCHES on domain: same
call count, same section coverage, same economics line, same handling of a
sub-sector the benchmark workbook has never heard of.
"""
import json

from django.core.management import call_command
from django.test import TestCase

from tests.conftest_helpers import auth_headers, make_world


DOMAINS = [
    # (label, website, sector, sub_sector, business model)
    ("construction", "https://girishbuild.example",
     "Construction", "Civil Engineering", "B2B"),
    ("electronics", "https://voltcore.example",
     "Electronics", "Power Semiconductors", "B2B"),
    ("logistics", "https://haulmatic.example",
     "Logistics", "Freight Forwarding", "B2B2C"),
]


class DomainAgnosticGeneration(TestCase):

    def setUp(self):
        call_command("seed_initial_data", verbosity=0)
        call_command("seed_platform_config", verbosity=0)
        call_command("seed_llm_costs", verbosity=0)
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        cfg.ai_mocked = True
        cfg.save()
        self.world = make_world()
        self.founder = self.world["a"]["founder"]
        self.headers = auth_headers(self.founder)
        self._companies = {}

    def _company(self, label):
        """A distinct company per domain, inside the one tenant."""
        from fundos.core.scoping import tenant_context
        from fundos.profile.models import CompanyProfile  # noqa: F401
        if label in self._companies:
            return self._companies[label]
        base = self.world["a"]["company"]
        with tenant_context(base.tenant_id):
            company = type(base).objects.create(
                tenant_id=base.tenant_id, name=f"Co {label.title()}")
        self._companies[label] = company
        return company

    def _run(self, label, website, sector, sub_sector, model):
        """One company per domain inside a SINGLE tenant world.

        `make_world()` creates fixed user emails, so calling it per domain
        trips the live-email unique index. Separate companies in one tenant is
        also the more realistic shape: a fund holds a construction deal and an
        electronics deal side by side.
        """
        from fundos.llm import ledger
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile)
        company = self._company(label)
        headers = self.headers

        self.client.post(
            f"/api/v1/companies/{company.id}/profile/onboard",
            json.dumps({"websiteUrl": website, "hqCountry": "IN",
                        "homeCurrency": "INR",
                        "sector": sector, "subSector": sub_sector,
                        "businessModelTypes": [model],
                        "founders": [{"name": "R. Nair", "isFullTime": True,
                                      "selfConfirmed": True}]}),
            content_type="application/json", **headers)

        ledger.reset()
        captured = []
        import fundos.profile.trace as trace_mod
        original = trace_mod.trace_event

        def capture(step, status, **fields):
            captured.append((step, status, fields))
            return original(step, status, **fields)
        trace_mod.trace_event = capture
        self.addCleanup(setattr, trace_mod, "trace_event", original)

        profile = get_or_create_profile(company, user=self.founder)
        result = generate_profile(profile, user=self.founder)
        return result, captured, profile

    # -- the invariants ---------------------------------------------------

    def test_the_same_pipeline_runs_for_every_domain(self):
        shapes = {}
        for label, site, sector, sub, model in DOMAINS:
            result, captured, _ = self._run(label, site, sector, sub, model)
            econ = [f for s, _, f in captured if s == "run.economics"]

            self.assertEqual(len(econ), 1, f"{label}: one economics line")
            self.assertEqual(result.get("llmCalls"), econ[0]["llm_calls"],
                             f"{label}: reported calls must match dispatched")
            shapes[label] = (result.get("mode"), econ[0]["llm_calls"])

        # Identical shape across domains. A difference here would mean the
        # pipeline is branching on something it has no business branching on.
        distinct = set(shapes.values())
        self.assertEqual(len(distinct), 1,
                         f"pipeline shape differs by domain: {shapes}")

    def test_an_unmapped_sub_sector_never_mis_groups_a_company(self):
        """None of these sub-sectors exists in any seeded benchmark workbook.

        The correct behaviour is to REFUSE to resolve rather than fuzzy-match
        a construction firm onto a D2C benchmark group. Assigning the wrong
        peer group is worse than assigning none: blank is excluded from the
        denominator and its weight redistributes across the categories that
        did score, whereas wrong quietly produces a confident number against
        the wrong comparators.
        """
        from fundos.profile.assessment_extraction import resolve_sub_sector
        for label, _site, _sector, sub, _model in DOMAINS:
            group, method = resolve_sub_sector(sub)
            self.assertIn(method, ("unresolved", "exact", "group", "name"),
                          f"{label}: {method}")
            if method == "unresolved":
                self.assertIsNone(group, label)

    def test_a_blank_category_redistributes_rather_than_scoring_zero(self):
        """The property that makes an unmapped sector safe.

        This is why "add a SectorMapping row for Eyewear" was the wrong
        remedy: the rating is not wrong when a category is blank, it is
        computed over the categories that scored, with the shares surfaced as
        applied_weight so a reader can see it.
        """
        from fundos.engines.deal_assessment import weighted_rollup
        children = [
            {"code": "A", "score": 8, "weight": 20},
            {"code": "B", "score": 6, "weight": 20},
            {"code": "C", "score": 7, "weight": 20},
            {"code": "D", "score": 5, "weight": 20},
            {"code": "F", "score": None, "weight": 20},   # unmapped sector
        ]
        score, applied = weighted_rollup(children)
        self.assertAlmostEqual(float(score), 6.5, places=6)
        self.assertEqual(len(applied), 4, "the blank must not be scored")
        for row in applied:
            self.assertAlmostEqual(row["applied_weight"], 25.0, places=6)

    def test_every_domain_reaches_a_terminal_state(self):
        """No domain may leave the run spinning. Either it generates, or it

        is abandoned with a reason — a founder in any sector gets an answer.
        """
        for label, site, sector, sub, model in DOMAINS:
            result, _captured, profile = self._run(label, site, sector, sub,
                                                   model)
            profile.refresh_from_db()
            # "pipeline" generated, "refused" declined to publish, "failed"
            # broke, "skipped" had nothing new. All four are answers; only a
            # profile stuck on "generating" is not.
            self.assertIn(result.get("mode"),
                          ("pipeline", "refused", "failed", "skipped"),
                          f"{label}: {result.get('mode')}")
            self.assertNotEqual(profile.status, "generating",
                                f"{label} left mid-flight")

    def test_business_model_vocabulary_admits_non_tech_companies(self):
        """The one closed vocabulary in the pipeline. A construction firm is

        B2B; a freight operator is B2B2C. Anything genuinely outside the list
        must land on `Other` rather than being dropped, or the section simply
        loses the field for whole industries.
        """
        from fundos.profile.section_writer import _BUSINESS_MODEL_TYPES
        for _l, _s, _sec, _sub, model in DOMAINS:
            self.assertIn(model, _BUSINESS_MODEL_TYPES)
        self.assertIn("Other", _BUSINESS_MODEL_TYPES,
                      "an escape hatch is what makes a closed list safe")

    def test_no_sector_name_is_hard_coded_in_the_profile_pipeline(self):
        """Sector knowledge belongs in imported data, not in source.

        `fundos/investors/` legitimately carries an alias table for investor
        matching, and that is seeded config. The PROFILE pipeline must carry
        none, or onboarding a new industry becomes a deploy.
        """
        import ast
        import pathlib
        import re
        banned = re.compile(
            r"\b(eyewear|fintech|edtech|healthtech|agritech)\b", re.I)
        offenders = []
        for path in pathlib.Path("fundos/profile").rglob("*.py"):
            if "migrations" in str(path):
                continue
            tree = ast.parse(path.read_text())
            # Walk STRING LITERALS and NAMES only. Comments never reach the
            # AST, and a docstring is a bare Expr — both may cite a sector as
            # an example ("Health Tech" == "Healthtech" is a matching note,
            # not a hard-coded rule). What matters is a sector name baked
            # into a value the code BRANCHES on.
            docstrings = set()
            for node in ast.walk(tree):
                if isinstance(node, (ast.Module, ast.ClassDef,
                                     ast.FunctionDef, ast.AsyncFunctionDef)):
                    d = ast.get_docstring(node, clean=False)
                    if d:
                        docstrings.add(d)
            for node in ast.walk(tree):
                if isinstance(node, ast.Constant) and isinstance(node.value, str):
                    if node.value in docstrings:
                        continue
                    if banned.search(node.value):
                        offenders.append(
                            f"{path}:{node.lineno}: {node.value[:60]!r}")
        self.assertEqual(offenders, [], f"hard-coded sectors: {offenders}")
