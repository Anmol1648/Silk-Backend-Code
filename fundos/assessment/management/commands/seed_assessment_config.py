"""Seed the Deal Assessment scoring model.

ANSWERING "DO I RUN THIS PER TENANT?" — NO, AND DELIBERATELY SO.
================================================================
The scoring model IS the product. Category weights, rubric cut-points and
anchor definitions were chosen by the product owner and are the same correct
answer for every tenant; a tyre marketplace and a healthcare app are both
scored against the same ₹-revenue-at-Series-A rubric, because that rubric is
a statement about fundraising, not about tyres.

So these rows are seeded ONCE, platform-wide, with `tenant_id = NULL`.
Every tenant reads them. Onboarding a new tenant requires NO seeding step
and no config work at all — they inherit the model and can start assessing
immediately.

A per-tenant seed would have been worse in three specific ways:
  1. A new tenant could start with no config and silently score nothing.
  2. Fixing a bad cut-point would mean updating N copies, and a tenant
     missed in that sweep drifts off the model without anyone noticing.
  3. Assessments across tenants stop being comparable, which kills the
     benchmarking the sector percentile model (§2.7) depends on.

If a tenant genuinely needs a different model — an advisor-facing variant
with the Mandate Context category, say — that is a deliberate FORK: insert
rows with their tenant_id set, and resolution prefers them. Almost no tenant
should. Treat a populated tenant_id as an exception requiring a reason.

USAGE
    python manage.py seed_assessment_config
    python manage.py seed_assessment_config --config-version 2 --activate
    python manage.py seed_assessment_config --check      # audit, no writes
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from fundos.assessment.spec_config_data import ANCHORS, RUBRICS

# §2.1 — the seven categories. Must sum to 100 (integrity check #1).
CATEGORIES = [
    ("A", "Team", 20),
    ("B", "Financials", 20),
    ("C", "Business Quality", 10),
    ("D", "Deal Dynamics", 10),
    ("E", "Sector Attractiveness", 10),
    ("F", "Sub-Sector Attractiveness", 20),
    ("G", "Mandate Context", 10),
]

# §2.3 — sub-item weights within each category (code, parent, label, weight).
SUBITEM_WEIGHTS = [
    # A. Team
    ("A.1", "A", "Founder Profile", 55), ("A.2", "A", "Leadership Team", 25),
    ("A.3", "A", "Board, Advisors & Investors", 20),
    # B. Financials
    ("B.1", "B", "Revenue Scale", 10), ("B.2", "B", "Revenue Growth", 15),
    ("B.3", "B", "Gross Margin (CM1)", 15),
    ("B.4", "B", "Contribution Margin (CM2)", 15),
    ("B.5", "B", "EBITDA Margin", 15), ("B.6", "B", "PAT Margin", 10),
    ("B.7", "B", "Cash Runway", 7.5),
    ("B.8", "B", "Working Capital Cycle", 7.5),
    ("B.9", "B", "Debt Level", 5),
    # C. Business Quality
    ("C.1", "C", "Company Age", 10),
    ("C.2", "C", "Revenue Predictability", 20),
    ("C.3", "C", "Competitive Moat", 20), ("C.4", "C", "Scalability", 15),
    ("C.5", "C", "Pricing Power", 10),
    ("C.6", "C", "Client Concentration", 10),
    ("C.7", "C", "Supplier Concentration", 10),
    ("C.8", "C", "Government Regulation", 5),
    # D. Deal Dynamics
    ("D.1", "D", "Raise vs. Last Round", 15),
    ("D.2", "D", "Raise vs. Total Raised", 15),
    ("D.3", "D", "Existing Investor Contribution", 20),
    ("D.4", "D", "Valuation Expectations", 15),
    ("D.5", "D", "Founder Dilution", 10),
    ("D.6", "D", "Founder Process Discipline", 5),
    ("D.7", "D", "Use of Proceeds", 10),
    ("D.8", "D", "Seller Motivation", 10),
    # E. Sector
    ("E.1", "E", "Market Size & Growth", 20), ("E.2", "E", "Deal Velocity", 17.5),
    ("E.3", "E", "Deal Ticket Size", 17.5), ("E.4", "E", "Peer Tenor", 7.5),
    ("E.5", "E", "Peer Raise Recency", 7.5),
    ("E.6", "E", "Active Investors", 17.5),
    ("E.7", "E", "News Flow & Sentiment", 12.5),
    # F. Sub-Sector
    ("F.1", "F", "Market Size & Growth", 20), ("F.2", "F", "Deal Velocity", 20),
    ("F.3", "F", "Deal Ticket Size", 20), ("F.4", "F", "Peer Tenor", 10),
    ("F.5", "F", "Peer Raise Recency", 10),
    ("F.6", "F", "Active Investors", 20),
    # G. Mandate Context
    ("G.1", "G", "Sector Knowledge & Relationships", 20),
    ("G.2", "G", "GTM Timeline", 20), ("G.3", "G", "Timeline to Close", 20),
    ("G.4", "G", "Team Capacity", 20), ("G.5", "G", "Deal Size", 20),
]

# Lettered children of the multi-child sub-items (§2.3).
CHILD_WEIGHTS = (
    [(f"A.1.{c}", "A.1", 20) for c in "abcde"]
    + [(f"A.2.{c}", "A.2", 25) for c in "abcd"]
    + [(f"A.3.{c}", "A.3", Decimal("33.3333")) for c in "abc"]
    + [(f"C.3.{c}", "C.3", 20) for c in "abcde"]
    + [("E.7.a", "E.7", 50), ("E.7.b", "E.7", 50)]
)

STAGES = ["Seed", "Series A", "Series B", "Growth"]

# ---------------------------------------------------------------------------
# CATEGORIES E AND F — 30% of every rating, and until v23 unscoreable.
#
# The spec extract carries four sector metrics under SHARED ref codes
# ("E.1 / F.1", "E.4 / F.4", "E.5 / F.5") because the workbook asks the same
# question twice: once of the sector, once of the sub-sector. It also relies
# on the Sector Deal Data table for velocity, ticket size and investor count,
# which are not questions at all — they are percentile scores read from the
# table Phase 2 imports.
#
# Neither survived derivation. A shared ref code split on "." produced the
# parent "E.1 / F", which matches no sub-item, so all four orphaned; and the
# three table-driven metrics had no parameter rows at all. The result was
# every sub-item of E except E.7, and the whole of F, carrying weight that
# nothing could ever score — 30% of the model silently redistributing itself
# across the other categories on every assessment.
#
# Declaring them explicitly here rather than inferring them is deliberate:
# the F-side is not a copy of the E-side, it is the same metric asked of a
# different population, and that distinction is exactly what category F is
# worth 20% for.
# ---------------------------------------------------------------------------

# Metrics asked of BOTH populations. (E-side key, F-side key, E ref, F ref).
# The F-side reuses the E-side's cut-points: the rubric expresses what a good
# TAM or a recent peer raise IS, which does not change with the width of the
# population being measured. Only the value does.
#
# THE SUFFIX IS LOAD-BEARING. The F-side key is the E-side key plus `_SUB`,
# never a `SUB_` prefix, because the workbook's own band formula opens with
# LET(key, SUBSTITUTE($C22,"_SUB",""), …) to find the rubric to band against.
# `SUB_TAM` matches nothing at all: the parameter exists, carries weight, and
# can never band — the same 30%-unscoreable failure this block was written to
# end, reintroduced one key at a time. `fundos.assessment.workbook` reads these
# straight from the sheet; this list is the offline twin and must agree with it.
SECTOR_MIRRORS = [
    ("SEC_TAM", "SEC_TAM_SUB", "E.1", "F.1",
     "Sub-Sector Total Addressable Market"),
    ("SEC_TAM_CAGR", "SEC_TAM_CAGR_SUB", "E.1", "F.1",
     "Sub-Sector Market CAGR"),
    ("SEC_PEER_AGE", "SEC_PEER_AGE_SUB", "E.4", "F.4",
     "Sub-Sector Median Peer Age"),
    ("SEC_PEER_RAISE_MTHS", "SEC_PEER_RAISE_MTHS_SUB", "E.5", "F.5",
     "Months Since Last Sub-Sector Peer Raise"),
]

# Table-driven metrics (§2.7). scoring_type='lookup': the stored value IS the
# 0-10 score, read from SectorDealData, so there are no cut-points to seed.
# Ranking happens once at import over the whole population — recomputing it
# per assessment would let a later partial refresh silently re-score a company
# nobody touched.
#
# Keyed `PCTL_<node with dots as underscores>`, matching what the workbook
# importer derives from the node tree, and flagged `is_workbook_input=False`:
# these six are COMPUTED from the deal database, not collected from a founder,
# so counting them would inflate the 80-row input contract the sheet measures
# coverage against.
# (key, ref/node, benchmark level, benchmark metric, name)
SECTOR_LOOKUPS = [
    ("PCTL_E_2", "E.2", "sector", "velocity", "Deal Velocity (percentile)"),
    ("PCTL_E_3", "E.3", "sector", "ticket",
     "Deal Ticket Size (percentile)"),
    ("PCTL_E_6", "E.6", "sector", "investors",
     "Active Investors (percentile)"),
    ("PCTL_F_2", "F.2", "sub_sector", "velocity",
     "Deal Velocity (percentile)"),
    ("PCTL_F_3", "F.3", "sub_sector", "ticket",
     "Deal Ticket Size (percentile)"),
    ("PCTL_F_6", "F.6", "sub_sector", "investors",
     "Active Investors (percentile)"),
]


def clean_ref(ref):
    """Normalise a workbook ref code to the one this row actually rolls into.

    Two shapes in the source needed handling, and both silently orphaned the
    parameter before v23:

      "C.6 (ref)"   -> "C.6"   a cross-check row, which must land in the SAME
                               parent as the parameter it cross-checks or the
                               contradiction test can never compare them.
      "E.1 / F.1"   -> "E.1"   a metric shared by two categories; the E-side
                               keeps the original key and the F-side is seeded
                               explicitly from SECTOR_MIRRORS.
    """
    text = str(ref or "").replace("(ref)", "").strip()
    if "/" in text:
        text = text.split("/")[0].strip()
    return text


def parent_of(ref):
    """The sub-item code a parameter rolls into, e.g. 'A.1.d' -> 'A.1'."""
    ref = clean_ref(ref)
    parts = ref.split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else ""


def _dec(v):
    if v in (None, "", "-"):
        return None
    try:
        return Decimal(str(v).replace(",", "").replace("%", "").strip())
    except Exception:
        return None


class Command(BaseCommand):
    help = ("Seed the platform-wide Deal Assessment scoring model "
            "(weights, rubrics, anchors, parameter dictionary).")

    def add_arguments(self, parser):
        # NB: not "--version" — argparse reserves that for Django itself and
        # registering it raises at import time.
        parser.add_argument("--config-version", type=int, default=1,
                            dest="config_version")
        parser.add_argument(
            "--activate", action="store_true",
            help="Deactivate previous versions once this one is written. "
                 "Without it the new version is seeded INACTIVE so it can be "
                 "reviewed before it starts re-rating live assessments.")
        parser.add_argument("--check", action="store_true",
                            help="Audit only. Writes nothing.")

    def handle(self, *args, **opts):
        version = opts["config_version"]
        if opts["check"]:
            return self._audit()

        with transaction.atomic():
            w = self._weights(version)
            r = self._rubrics(version)
            a = self._anchors(version)
            p = self._parameters(version)
            if opts["activate"]:
                self._activate(version)

        self.stdout.write(self.style.SUCCESS(
            f"  assessment config v{version}: {w} weights, {r} rubrics, "
            f"{a} anchors, {p} parameters"))
        if not opts["activate"]:
            self.stdout.write(self.style.WARNING(
                "  Seeded but NOT activated. Review, then re-run with "
                "--activate. Activating changes scores on the next run."))
        self._audit()

    # -- writers ---------------------------------------------------------
    def _weights(self, version):
        from fundos.assessment.models import ConfigWeight
        n = 0
        rows = ([("category", c, "", label, wt) for c, label, wt in CATEGORIES]
                + [("subitem", c, p, label, wt)
                   for c, p, label, wt in SUBITEM_WEIGHTS]
                + [("child", c, p, "", wt) for c, p, wt in CHILD_WEIGHTS])
        for level, code, parent, label, weight in rows:
            _, made = ConfigWeight.objects.get_or_create(
                tenant_id=None, level=level, code=code, version=version,
                defaults={"parent_code": parent, "label": label,
                          "weight": Decimal(str(weight)), "is_active": False})
            n += int(made)
        return n

    def _rubrics(self, version):
        from fundos.assessment.models import ConfigRubric
        n = 0
        for r in RUBRICS:
            for stage in STAGES:
                cuts = (r.get("stage_cuts") or {}).get(stage)
                if not cuts:
                    continue
                kw = dict(metric_name=r["name"], unit=r["unit"],
                          direction=r["direction"], ref_code=r["ref_code"],
                          rationale=r.get("rationale", ""), is_active=False)
                if r["direction"] == "Range":
                    # Range rows carry [idealMin, idealMax]; tolerance bands
                    # are the spec's defaults (§2.4.3b) unless overridden.
                    kw.update(ideal_min=_dec(cuts[0]),
                              ideal_max=_dec(cuts[1] if len(cuts) > 1
                                             else cuts[0]),
                              good_tolerance_pct=Decimal("25"),
                              fair_tolerance_pct=Decimal("50"))
                else:
                    kw.update(cut_excellent=_dec(cuts[0]),
                              cut_good=_dec(cuts[1] if len(cuts) > 1 else None),
                              cut_fair=_dec(cuts[2] if len(cuts) > 2 else None))
                _, made = ConfigRubric.objects.get_or_create(
                    tenant_id=None, input_key=r["input_key"], stage=stage,
                    version=version, defaults=kw)
                n += int(made)
        n += self._mirror_rubrics(version)
        return n

    def _mirror_rubrics(self, version):
        """Give each sub-sector metric the cut-points of its sector twin.

        The rubric states what a good TAM or a recent peer raise IS; that
        judgement does not change with the width of the population being
        measured, only the value does. Copying rather than re-authoring keeps
        one source of truth, so tightening the sector cut-point in the spec
        moves the sub-sector one with it and the two can never drift apart.
        """
        from fundos.assessment.models import ConfigRubric
        n = 0
        for src_key, dst_key, _e_ref, f_ref, name in SECTOR_MIRRORS:
            for src in ConfigRubric.objects.filter(tenant_id=None,
                                                   input_key=src_key,
                                                   version=version):
                _, made = ConfigRubric.objects.get_or_create(
                    tenant_id=None, input_key=dst_key, stage=src.stage,
                    version=version,
                    defaults=dict(
                        ref_code=f_ref, metric_name=name, unit=src.unit,
                        direction=src.direction,
                        cut_excellent=src.cut_excellent,
                        cut_good=src.cut_good, cut_fair=src.cut_fair,
                        ideal_min=src.ideal_min, ideal_max=src.ideal_max,
                        good_tolerance_pct=src.good_tolerance_pct,
                        fair_tolerance_pct=src.fair_tolerance_pct,
                        rationale=(
                            f"{src.rationale} Applied to the sub-sector "
                            f"population rather than the sector."),
                        is_active=False))
                n += int(made)
        return n

    def _anchors(self, version):
        from fundos.assessment.models import ConfigAnchor
        n = 0
        for a in ANCHORS:
            _, made = ConfigAnchor.objects.get_or_create(
                tenant_id=None, input_key=a["input_key"], version=version,
                defaults=dict(
                    ref_code=a.get("ref_code", ""),
                    parameter_name=a.get("parameter_name", ""),
                    scoring_basis=a.get("scoring_basis", ""),
                    excellent_def=a.get("excellent_def", ""),
                    good_def=a.get("good_def", ""),
                    fair_def=a.get("fair_def", ""),
                    poor_def=a.get("poor_def", ""),
                    evidence_required=a.get("evidence_required", ""),
                    is_active=False))
            n += int(made)
        return n

    def _parameters(self, version):
        """Derive the parameter dictionary from the rubric + anchor sets.

        Deriving rather than re-listing keeps one source of truth: a
        parameter cannot exist with no way to score it, and a rubric cannot
        exist for a parameter nobody declared.
        """
        from fundos.assessment.models import ConfigParameter
        n = 0
        seen = set()

        def write(key, ref, name, unit, scoring_type, feeds_score, **extra):
            nonlocal n
            if key in seen:
                return
            seen.add(key)
            ref = clean_ref(ref)
            defaults = dict(
                ref_code=ref, category_code=(ref[:1] if ref else ""),
                parent_code=parent_of(ref), name=name, unit=unit,
                scoring_type=scoring_type, feeds_score=feeds_score,
                is_active=False)
            defaults.update(extra)   # a caller may override any of the above
            _, made = ConfigParameter.objects.get_or_create(
                tenant_id=None, input_key=key, version=version,
                defaults=defaults)
            n += int(made)

        for r in RUBRICS:
            write(r["input_key"], r.get("ref_code", ""), r["name"],
                  r["unit"],
                  "range" if r["direction"] == "Range" else "numeric",
                  not r.get("is_reference_only"))
        for a in ANCHORS:
            write(a["input_key"], a.get("ref_code", ""),
                  a.get("parameter_name", ""), "", "anchor",
                  "Numeric-primary" not in (a.get("scoring_basis") or ""))

        # The sub-sector half of each shared metric (category F, 20%).
        by_key = {r["input_key"]: r for r in RUBRICS}
        for src_key, dst_key, _e_ref, f_ref, name in SECTOR_MIRRORS:
            src = by_key.get(src_key)
            if src is None:
                continue
            write(dst_key, f_ref, name, src["unit"],
                  "range" if src["direction"] == "Range" else "numeric", True)

        # The table-driven percentile metrics (E.2/E.3/E.6, F.2/F.3/F.6).
        # `parent_code` is the node itself, not its two-part prefix: each of
        # these IS a scored leaf sitting directly under its sub-item, which is
        # also how the workbook importer writes them.
        for key, ref, level, metric, name in SECTOR_LOOKUPS:
            write(key, ref, name, "Score", "lookup", True,
                  parent_code=clean_ref(ref), is_workbook_input=False,
                  benchmark_level=level, benchmark_metric=metric)
        return n

    def _activate(self, version):
        from fundos.assessment.models import (ConfigAnchor, ConfigParameter,
                                              ConfigRubric, ConfigWeight)
        for model in (ConfigWeight, ConfigRubric, ConfigAnchor,
                      ConfigParameter):
            model.objects.filter(tenant_id=None).exclude(
                version=version).update(is_active=False)
            model.objects.filter(tenant_id=None, version=version).update(
                is_active=True)

    # -- audit -----------------------------------------------------------
    def _audit(self):
        """Integrity checks that must hold before any score is trustworthy."""
        from fundos.assessment.models import (ConfigAnchor, ConfigParameter,
                                              ConfigRubric, ConfigWeight)
        problems = []

        cat_sum = sum(
            (w.weight for w in ConfigWeight.objects.filter(
                tenant_id=None, level="category", is_active=True)),
            Decimal("0"))
        if ConfigWeight.objects.filter(level="category",
                                       is_active=True).exists():
            if cat_sum != 100:
                problems.append(
                    f"CHECK 1 FAILED: category weights sum to {cat_sum}, "
                    f"not 100. Every score built on this is wrong.")
            else:
                self.stdout.write(self.style.SUCCESS(
                    "  check 1: category weights sum to 100 [OK]"))

        # Every scored parameter needs a way to be scored.
        for p in ConfigParameter.objects.filter(tenant_id=None,
                                                is_active=True,
                                                feeds_score=True):
            if p.scoring_type in ("numeric", "range"):
                if not ConfigRubric.objects.filter(
                        input_key=p.input_key, is_active=True).exists():
                    problems.append(
                        f"{p.input_key}: scored numeric parameter with no "
                        f"rubric — it can never be banded.")
            elif p.scoring_type == "anchor":
                if not ConfigAnchor.objects.filter(
                        input_key=p.input_key, is_active=True).exists():
                    problems.append(
                        f"{p.input_key}: scored anchor parameter with no "
                        f"band definitions.")

        # A rubric missing a stage silently un-scores that parameter for
        # every company at that stage — invisible unless checked.
        for key in ConfigRubric.objects.filter(
                tenant_id=None, is_active=True).values_list(
                    "input_key", flat=True).distinct():
            have = set(ConfigRubric.objects.filter(
                tenant_id=None, input_key=key, is_active=True
            ).values_list("stage", flat=True))
            missing = [s for s in STAGES if s not in have]
            if missing:
                problems.append(
                    f"{key}: no cut-points for {', '.join(missing)} — "
                    f"companies at that stage score blank on this row.")

        # THE CHECK THAT WOULD HAVE CAUGHT E AND F.
        #
        # A sub-item carrying weight with no parameter beneath it is invisible
        # in every other check: the weights still sum to 100, every parameter
        # still has a rubric, every rubric still has four stages. It simply
        # never scores, and `weighted_rollup` redistributes its share to its
        # siblings without comment. Categories E and F shipped that way — 30%
        # of every rating quietly reallocated — and nothing reported it.
        subitems = {w.code: w for w in ConfigWeight.objects.filter(
            tenant_id=None, level="subitem", is_active=True)}
        parented = {p.parent_code or p.category_code
                    for p in ConfigParameter.objects.filter(
                        tenant_id=None, is_active=True)}
        stranded = sorted(set(subitems) - parented)
        if stranded:
            share = sum(float(subitems[c].weight) for c in stranded)
            problems.append(
                f"{len(stranded)} sub-item(s) carry weight but have no "
                f"parameter beneath them: {', '.join(stranded[:8])}. Their "
                f"share ({share:g} weight-points across their categories) can "
                f"never be scored and silently redistributes to their "
                f"siblings on every assessment.")
        else:
            self.stdout.write(self.style.SUCCESS(
                "  check 2: every weighted sub-item has parameters [OK]"))

        # A parameter whose parent matches nothing rolls up nowhere. It looks
        # answered on the scorecard and contributes to no category.
        known = set(subitems) | {w.code for w in ConfigWeight.objects.filter(
            tenant_id=None, level="category", is_active=True)}
        orphans = [p.input_key for p in ConfigParameter.objects.filter(
            tenant_id=None, is_active=True)
            if (p.parent_code or p.category_code) not in known]
        if orphans:
            problems.append(
                f"{len(orphans)} parameter(s) roll up to a node that does not "
                f"exist: {', '.join(orphans[:8])}. They are collected and "
                f"scored but contribute to no category.")
        else:
            self.stdout.write(self.style.SUCCESS(
                "  check 3: every parameter rolls up to a real node [OK]"))

        if problems:
            self.stdout.write(self.style.ERROR(
                f"\n  {len(problems)} config problem(s):"))
            for p in problems[:25]:
                self.stdout.write(self.style.ERROR(f"    - {p}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "  config integrity: all checks passed"))
        return problems
