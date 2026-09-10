"""Stage 2 review — deterministic, and deliberately not a model call.

WHY THE LLM WAS REMOVED FROM HERE
---------------------------------
`run_rubric_review` asked a model to look at the finished scorecard and, at
high confidence, RE-BAND parameters the rules had already banded. That is the
one thing scoring must never do.

The whole value of this workbook is that a score can be defended: this figure
came from that source, this threshold from the published rubric, and the
arithmetic between them is fixed. A model re-banding at the end breaks the
chain in the least visible way possible — the number changes, the audit trail
still points at a rubric that no longer produced it, and the same inputs can
score differently on Tuesday than they did on Monday.

Everything the review was asked to catch is expressible as a rule, because the
workbook already states the rules. What is left is arithmetic and comparison,
and both belong in code:

  * a value outside any plausible range for its unit
  * a reference row contradicting the scored row it cross-checks
  * a stage that was defaulted rather than chosen
  * coverage too thin for the headline to mean anything
  * a category resting on one answered parameter out of many
  * a percentile drawn from a sample below the workbook's own floor

The LLM's remaining job in this system is STAGE 1: reading unstructured
sources — a website, a PDF financial model — and turning them into typed,
sourced values. That is genuine language work. Deciding whether 14 months of
runway is Fair at Series A is not: it is a comparison against a number in a
spreadsheet, and a model adds only variance.
"""
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

# Sanity envelopes by unit. Deliberately WIDE — these catch a unit or scale
# error (a percentage recorded as 4500, revenue in rupees where the workbook
# wants crore), not an unusual company. A narrow band here would start
# second-guessing the rubric, which is exactly what this module refuses to do.
UNIT_RANGES = {
    "%": (Decimal("-100"), Decimal("1000")),
    "Years": (Decimal("0"), Decimal("100")),
    "Months": (Decimal("0"), Decimal("600")),
    "Count": (Decimal("0"), Decimal("100000")),
    "x": (Decimal("0"), Decimal("1000")),
    "Score": (Decimal("0"), Decimal("10")),
}

# Reference rows and the scored row each cross-checks. Both come from the
# workbook: a top-1 client share that exceeds the top-5 share is arithmetically
# impossible, and a top-1 share that is most of the top-5 means concentration
# is worse than the scored row implies.
CONTRADICTIONS = [
    ("BQ_TOP1_CLIENT", "BQ_TOP5_CLIENT", "top_1_exceeds_top_5",
     "The largest client accounts for {a}% of revenue but the top five "
     "account for {b}%. The first figure cannot exceed the second, so at "
     "least one is wrong."),
]


def review(assessment, tenant_id=None):
    """Every deterministic check, as a list of findings. Never raises.

    Findings describe; they do not re-score. A reader can act on them, and the
    number beside them is still the number the rules produced.
    """
    tenant_id = tenant_id or getattr(assessment, "tenant_id", None)
    values = {pv.input_key: pv for pv in assessment.parameter_values.all()}
    findings = []
    for check in (_implausible_values, _contradictions, _thin_categories,
                  _percentile_samples, _overrides):
        try:
            findings.extend(check(assessment, values, tenant_id))
        except Exception as e:      # a broken check must not lose the others
            logger.warning("REVIEW %s: %s failed: %s", assessment.id,
                           check.__name__, e, exc_info=True)
    logger.info("REVIEW %s: %d deterministic finding(s)", assessment.id,
                len(findings))
    return findings


def _num(raw):
    try:
        return Decimal(str(raw).replace(",", "").replace("%", "").strip())
    except (InvalidOperation, AttributeError, ValueError):
        return None


def _implausible_values(assessment, values, tenant_id):
    """A figure outside any sane envelope for its unit.

    Almost always a unit error rather than a remarkable company: a churn rate
    of 4500 is 45% recorded as basis points, and it will band Poor and drag a
    category with complete confidence unless something says so.
    """
    out = []
    for pv in values.values():
        value = _num(pv.raw_value)
        if value is None:
            continue
        low, high = UNIT_RANGES.get((pv.unit or "").strip(), (None, None))
        if low is None or low <= value <= high:
            continue
        out.append({
            "source": "rules", "check": "implausible_value",
            "severity": "warning", "category": pv.category or "",
            "inputKey": pv.input_key,
            "message": (
                f"{pv.input_key} is {pv.raw_value} {pv.unit}, outside the "
                f"plausible range {low:g}–{high:g}. This is usually a unit or "
                f"scale error, and it will band and score as though it were "
                f"correct."),
        })
    return out


def _contradictions(assessment, values, tenant_id):
    """A reference row disagreeing with the row it cross-checks."""
    out = []
    for ref_key, scored_key, code, template in CONTRADICTIONS:
        a, b = _num(values.get(ref_key, None) and values[ref_key].raw_value), \
            _num(values.get(scored_key, None) and values[scored_key].raw_value)
        if a is None or b is None:
            continue
        if a > b:
            out.append({
                "source": "rules", "check": code, "severity": "warning",
                "category": values[scored_key].category or "",
                "inputKey": scored_key,
                "message": template.format(a=a, b=b)})
        elif b > 0 and a / b >= Decimal("0.8"):
            out.append({
                "source": "rules", "check": "concentration_understated",
                "severity": "info",
                "category": values[scored_key].category or "",
                "inputKey": scored_key,
                "message": (
                    f"The largest client is {a}% of revenue against {b}% for "
                    f"the top five, so the concentration sits almost entirely "
                    f"in one relationship. The scored figure reads better "
                    f"than the position is.")})
    return out


def _thin_categories(assessment, values, tenant_id):
    """A category scoring off one or two answers out of many.

    The category score is an average over what was answered, so a single
    Excellent in an otherwise empty category produces an Excellent category —
    arithmetically correct and materially misleading.
    """
    from fundos.assessment.models import ConfigParameter

    expected = {}
    for p in ConfigParameter.objects.filter(is_active=True, feeds_score=True):
        expected.setdefault(p.category_code or "?", []).append(p.input_key)

    out = []
    for code, keys in expected.items():
        answered = [k for k in keys
                    if k in values and values[k].score is not None]
        if not answered or len(keys) < 4:
            continue
        ratio = len(answered) / len(keys)
        if ratio <= 0.25:
            out.append({
                "source": "rules", "check": "thin_category",
                "severity": "warning", "category": code,
                "message": (
                    f"Category {code} scored on {len(answered)} of "
                    f"{len(keys)} parameters. The category average is drawn "
                    f"from that handful, so it reflects those answers rather "
                    f"than the category.")})
    return out


def _percentile_samples(assessment, values, tenant_id):
    """A percentile whose population is thinner than the workbook allows."""
    from fundos.assessment.models import ConfigParameter, SectorDealData
    from fundos.assessment.services import constant

    floor = int(constant("min_deals_for_percentile", 5, tenant_id) or 5)
    lookups = {p.input_key: p for p in ConfigParameter.objects.filter(
        is_active=True, scoring_type="lookup")}
    out = []
    for key, pv in values.items():
        cfg = lookups.get(key)
        if cfg is None or pv.score is None:
            continue
        name = (assessment.sector if cfg.benchmark_level == "sector"
                else assessment.sub_sector)
        row = SectorDealData.objects.filter(
            level=cfg.benchmark_level, name__iexact=(name or "")).first()
        if row is None or (row.deal_count or 0) >= floor:
            continue
        out.append({
            "source": "rules", "check": "thin_benchmark_sample",
            "severity": "warning", "category": cfg.category_code,
            "inputKey": key,
            "message": (
                f"{row.name} carries {row.deal_count} deals, below the "
                f"{floor} the methodology requires for a percentile to mean "
                f"anything. This row should not have scored.")})
    return out


def _overrides(assessment, values, tenant_id):
    """Human overrides, surfaced rather than buried.

    Not a criticism — an override is often the most informed number on the
    sheet. But a reader comparing two assessments needs to know which figures
    a person moved and which the rules produced.
    """
    moved = [pv for pv in values.values() if pv.is_overridden]
    if not moved:
        return []
    return [{
        "source": "rules", "check": "manual_overrides",
        "severity": "info", "category": "",
        "message": (
            f"{len(moved)} parameter(s) were overridden by hand: "
            f"{', '.join(sorted(pv.input_key for pv in moved)[:8])}. The "
            f"rule-based result is retained beside each.")}]
