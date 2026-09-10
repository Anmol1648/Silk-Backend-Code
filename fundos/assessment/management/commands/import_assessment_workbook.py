"""Import the assessment workbook AS THE SCORING CONFIGURATION.

This replaces `seed_assessment_config`, which seeded from a hand-transcribed
copy of the spec. Config is now read from the workbook that is authoritative,
so a workbook revision is an import rather than a release, and the two copies
can no longer drift apart.

    python manage.py import_assessment_workbook path/to/workbook.xlsx --check
    python manage.py import_assessment_workbook path/to/workbook.xlsx \
        --config-version 3 --activate

Activating changes scores. Import without --activate first and read the audit.
"""
from decimal import Decimal

from django.core.management.base import BaseCommand
from django.db import transaction

from fundos.assessment.workbook import WorkbookError, parse_config_workbook

STAGES = ["Seed", "Series A", "Series B", "Growth"]


class Command(BaseCommand):
    help = ("Read the assessment workbook's configuration sheets — the input "
            "contract, rubrics, anchors, node tree and constants — into "
            "versioned config.")

    def add_arguments(self, parser):
        parser.add_argument("path", help="Path to the .xlsx workbook.")
        parser.add_argument("--config-version", type=int, default=1,
                            dest="config_version")
        parser.add_argument(
            "--activate", action="store_true",
            help="Deactivate previous versions once this one is written. "
                 "Without it the version is seeded INACTIVE so it can be "
                 "reviewed before it starts re-rating live assessments.")
        parser.add_argument("--check", action="store_true",
                            help="Parse and audit only. Writes nothing.")

    def handle(self, *args, **opts):
        try:
            data = parse_config_workbook(opts["path"])
        except WorkbookError as e:
            self.stderr.write(self.style.ERROR(f"Refused to import: {e}"))
            raise SystemExit(1)

        self._report(data["report"])
        if opts["check"]:
            return

        version = opts["config_version"]
        with transaction.atomic():
            counts = self._write(data, version)
            if opts["activate"]:
                self._activate(version)

        self.stdout.write(self.style.SUCCESS(
            f"\n  config v{version}: {counts['parameters']} parameters, "
            f"{counts['rubrics']} rubric rows, {counts['anchors']} anchors, "
            f"{counts['weights']} weights, {counts['constants']} constants"))
        if not opts["activate"]:
            self.stdout.write(self.style.WARNING(
                "  Seeded but NOT activated. Review, then re-run with "
                "--activate. Activating changes scores on the next run."))
        self._audit()

    # -- reporting -------------------------------------------------------
    def _report(self, r):
        self.stdout.write("Parsed the workbook:")
        for label, key in (("input contract rows", "inputs"),
                           ("monotonic rubrics", "monotonic_rubrics"),
                           ("range rubrics", "range_rubrics"),
                           ("qualitative anchors", "anchors"),
                           ("categories", "categories"),
                           ("sub-items", "subitems"),
                           ("children", "children"),
                           ("scored leaves", "scored_leaves"),
                           ("percentile nodes", "percentile_nodes"),
                           ("data dictionary rows", "dictionary_rows")):
            self.stdout.write(f"    {label:24} {r[key]}")
        self.stdout.write("  constants:")
        for k, v in sorted(r["constants"].items()):
            self.stdout.write(f"    {k:28} {v:g}")
        if r["unscored_inputs"]:
            self.stdout.write(
                f"  {len(r['unscored_inputs'])} input(s) are collected but "
                f"feed no score — reference rows and joint-scored companions:")
            self.stdout.write(
                "    " + ", ".join(r["unscored_inputs"][:12])
                + (" …" if len(r["unscored_inputs"]) > 12 else ""))

    # -- writing ---------------------------------------------------------
    def _write(self, data, version):
        from fundos.assessment.models import (ConfigAnchor, ConfigConstant,
                                              ConfigParameter, ConfigRubric,
                                              ConfigWeight)

        counts = {"parameters": 0, "rubrics": 0, "anchors": 0, "weights": 0,
                  "constants": 0}

        # A version is rewritten from scratch rather than merged. Merging
        # would leave a parameter deleted from the workbook alive in config,
        # which is exactly how the previous copy drifted.
        for model in (ConfigParameter, ConfigRubric, ConfigAnchor,
                      ConfigWeight, ConfigConstant):
            model.objects.filter(tenant_id=None, version=version).delete()

        for c in data["categories"]:
            ConfigWeight.objects.create(
                tenant_id=None, version=version, level="category",
                code=c["code"], parent_code="", label=c["label"],
                weight=c["weight"], is_active=False)
            counts["weights"] += 1
        for s in data["subitems"]:
            ConfigWeight.objects.create(
                tenant_id=None, version=version, level="subitem",
                code=s["code"], parent_code=s["parent_code"],
                label=s["label"], weight=s["weight"], is_active=False)
            counts["weights"] += 1
        for ch in data["children"]:
            ConfigWeight.objects.create(
                tenant_id=None, version=version, level="child",
                code=ch["code"], parent_code=ch["parent_code"],
                label=ch["label"], weight=ch["weight"], is_active=False)
            counts["weights"] += 1

        # Which node each collected key rolls into, and which keys score.
        node_for = {lf["input_key"]: lf["node"]
                    for lf in data["leaves"] if lf.get("input_key")}
        anchor_keys = data["anchor_keys"]
        ranges = {r["input_key"] for r in data["ranges"]}
        dictionary = data["dictionary"]

        # E.1 / F.1 are scored on the WORSE of TAM size and TAM growth. The
        # growth key is collected but rolls into no node of its own, so it is
        # attached to its companion rather than left unscored.
        companions = {}
        for key in ("SEC_TAM", "SEC_TAM_SUB"):
            partner = key.replace("SEC_TAM", "SEC_TAM_CAGR")
            if partner in {i["input_key"] for i in data["inputs"]}:
                companions[key] = partner

        for row in data["inputs"]:
            key = row["input_key"]
            ref = row["ref_code"]
            node = node_for.get(key, "")
            guide = dictionary.get(ref, {})
            scoring_type = ("anchor" if key in anchor_keys
                            else "range" if key in ranges else "numeric")
            ConfigParameter.objects.create(
                tenant_id=None, version=version, input_key=key,
                ref_code=ref, category_code=(ref[:1] if ref else ""),
                # The roll-up groups leaves by their SUB-ITEM, with the
                # child weight looked up on the ref code. A leaf that is
                # itself a sub-item (B.1, E.2) sits directly under its own
                # code and becomes that sub-item's only child.
                parent_code=(_owning_subitem(node) if node
                             else _parent_of(ref)),
                name=row["name"], unit=row["unit"],
                scoring_type=scoring_type,
                feeds_score=bool(node),
                is_workbook_input=True,
                companion_key=companions.get(key, ""),
                scoring_method=guide.get("scoring_method", "")[:32],
                definition=guide.get("definition", ""),
                where_to_find=guide.get("where_to_find", ""),
                if_missing=guide.get("if_missing", ""),
                sort_order=row["row"], is_active=False)
            counts["parameters"] += 1

        # The six nodes the workbook computes from the deal database. They
        # are NOT part of the 80-row contract and are flagged so, or the
        # sheet's own "inputs captured (of 80)" count stops meaning anything.
        for lf in data["leaves"]:
            if not lf.get("benchmark_metric"):
                continue
            code = lf["node"]
            ConfigParameter.objects.create(
                tenant_id=None, version=version,
                input_key=f"PCTL_{code.replace('.', '_')}",
                ref_code=code, category_code=lf["category_code"],
                parent_code=code, name=f"{lf['label']} (percentile)",
                unit="Score", scoring_type="lookup", feeds_score=True,
                is_workbook_input=False,
                benchmark_level=lf["benchmark_level"],
                benchmark_metric=lf["benchmark_metric"],
                is_active=False)
            counts["parameters"] += 1

        for m in data["monotonic"]:
            for stage in STAGES:
                a, b, c = m["stage_cuts"].get(stage, (None, None, None))
                if a is None and b is None and c is None:
                    continue
                ConfigRubric.objects.create(
                    tenant_id=None, version=version,
                    input_key=m["input_key"], stage=stage,
                    ref_code=m["ref_code"], metric_name=m["metric_name"],
                    unit=m["unit"], direction=m["direction"],
                    cut_excellent=a, cut_good=b, cut_fair=c,
                    rationale=m["rationale"], is_active=False)
                counts["rubrics"] += 1
        for r in data["ranges"]:
            for stage in STAGES:
                lo, hi = r["stage_bands"].get(stage, (None, None))
                if lo is None and hi is None:
                    continue
                ConfigRubric.objects.create(
                    tenant_id=None, version=version,
                    input_key=r["input_key"], stage=stage,
                    ref_code=r["ref_code"], metric_name=r["metric_name"],
                    unit=r["unit"], direction="Range",
                    ideal_min=lo, ideal_max=hi,
                    # Stored as whole percents; the sheet holds proportions.
                    good_tolerance_pct=(r["good_tol"] * 100
                                        if r["good_tol"] is not None else None),
                    fair_tolerance_pct=(r["fair_tol"] * 100
                                        if r["fair_tol"] is not None else None),
                    rationale=r["rationale"], is_active=False)
                counts["rubrics"] += 1

        for a in data["anchors"]:
            ConfigAnchor.objects.create(
                tenant_id=None, version=version, is_active=False, **a)
            counts["anchors"] += 1

        counts["rubrics"] += self._mirror_sub_rubrics(data, version)

        for key, value in data["constants"].items():
            ConfigConstant.objects.create(
                tenant_id=None, version=version, key=key,
                value=Decimal(str(value)), is_active=False)
            counts["constants"] += 1

        return counts

    def _mirror_sub_rubrics(self, data, version):
        """Give every `_SUB` key the cut-points of its base key.

        This is not an invention — it is what the sheet itself does. The band
        formula on every Company Profile row opens with

            LET(key, SUBSTITUTE($C22,"_SUB",""), …)

        so `SEC_TAM_SUB` is banded against `SEC_TAM`'s rubric. The judgement a
        rubric encodes — what a good TAM or a recent peer raise IS — does not
        change with the width of the population being measured. Only the value
        does, which is the whole point of asking twice.
        """
        from fundos.assessment.models import ConfigRubric

        n = 0
        for row in data["inputs"]:
            key = row["input_key"]
            if not key.endswith("_SUB"):
                continue
            base = key.replace("_SUB", "")
            for src in ConfigRubric.objects.filter(tenant_id=None,
                                                   version=version,
                                                   input_key=base):
                ConfigRubric.objects.create(
                    tenant_id=None, version=version, input_key=key,
                    stage=src.stage, ref_code=row["ref_code"],
                    metric_name=row["name"], unit=src.unit,
                    direction=src.direction,
                    cut_excellent=src.cut_excellent, cut_good=src.cut_good,
                    cut_fair=src.cut_fair, ideal_min=src.ideal_min,
                    ideal_max=src.ideal_max,
                    good_tolerance_pct=src.good_tolerance_pct,
                    fair_tolerance_pct=src.fair_tolerance_pct,
                    rationale=(f"{src.rationale} Applied to the sub-sector "
                               f"population rather than the sector."),
                    is_active=False)
                n += 1
        return n

    def _activate(self, version):
        from fundos.assessment.models import (ConfigAnchor, ConfigConstant,
                                              ConfigParameter, ConfigRubric,
                                              ConfigWeight)
        for model in (ConfigWeight, ConfigRubric, ConfigAnchor,
                      ConfigParameter, ConfigConstant):
            model.objects.filter(tenant_id=None).exclude(
                version=version).update(is_active=False)
            model.objects.filter(tenant_id=None, version=version).update(
                is_active=True)

    # -- audit -----------------------------------------------------------
    def _audit(self):
        from fundos.assessment.models import (ConfigAnchor, ConfigParameter,
                                              ConfigRubric, ConfigWeight)
        problems = []

        cat = ConfigWeight.objects.filter(tenant_id=None, level="category",
                                          is_active=True)
        total = sum((w.weight for w in cat), Decimal("0"))
        if cat.exists() and total != 100:
            problems.append(f"category weights sum to {total}, not 100.")
        elif cat.exists():
            self.stdout.write(self.style.SUCCESS(
                "  check 1: category weights sum to 100 ✓"))

        subitems = {w.code for w in ConfigWeight.objects.filter(
            tenant_id=None, level="subitem", is_active=True)}
        parented = {p.parent_code or p.category_code
                    for p in ConfigParameter.objects.filter(
                        tenant_id=None, is_active=True)}
        stranded = sorted(subitems - parented)
        if stranded:
            problems.append(
                f"{len(stranded)} sub-item(s) carry weight with no parameter "
                f"beneath them: {', '.join(stranded[:8])}.")
        elif subitems:
            self.stdout.write(self.style.SUCCESS(
                "  check 2: every weighted sub-item has parameters ✓"))

        contract = ConfigParameter.objects.filter(
            tenant_id=None, is_active=True, is_workbook_input=True).count()
        if contract and contract != 80:
            problems.append(
                f"the input contract holds {contract} rows, not the 80 the "
                f"workbook counts coverage against.")
        elif contract:
            self.stdout.write(self.style.SUCCESS(
                "  check 3: the input contract is exactly 80 rows ✓"))

        for p in ConfigParameter.objects.filter(tenant_id=None,
                                                is_active=True,
                                                feeds_score=True):
            if p.scoring_type in ("numeric", "range"):
                if not ConfigRubric.objects.filter(
                        input_key=p.input_key, is_active=True).exists():
                    problems.append(f"{p.input_key}: no rubric — it can "
                                    f"never be banded.")
            elif p.scoring_type == "anchor":
                if not ConfigAnchor.objects.filter(
                        input_key=p.input_key, is_active=True).exists():
                    problems.append(f"{p.input_key}: no band definitions.")

        for key in ConfigRubric.objects.filter(
                tenant_id=None, is_active=True).values_list(
                    "input_key", flat=True).distinct():
            have = set(ConfigRubric.objects.filter(
                tenant_id=None, input_key=key, is_active=True
            ).values_list("stage", flat=True))
            missing = [s for s in STAGES if s not in have]
            if missing:
                problems.append(
                    f"{key}: no cut-points for {', '.join(missing)}.")

        if problems:
            self.stdout.write(self.style.ERROR(
                f"\n  {len(problems)} config problem(s):"))
            for p in problems[:25]:
                self.stdout.write(self.style.ERROR(f"    - {p}"))
        else:
            self.stdout.write(self.style.SUCCESS(
                "  config integrity: all checks passed"))
        return problems


def _owning_subitem(node):
    """The sub-item a leaf rolls into.

    'A.1.d' -> 'A.1' (one of five weighted children of Founder Profile)
    'B.1'   -> 'B.1' (the sub-item IS the leaf, and becomes its own child)
    """
    parts = str(node or "").split(".")
    return node if len(parts) <= 2 else ".".join(parts[:2])


def _parent_of(ref):
    ref = str(ref or "").replace("(ref)", "").strip()
    parts = ref.split(".")
    return ".".join(parts[:2]) if len(parts) > 1 else ""
