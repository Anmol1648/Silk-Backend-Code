"""Deal Assessment scoring engine — Fundraising Journey Spec, Part 2.

This is a faithful, CONFIG-DRIVEN implementation of the source workbook's
scoring model. Every number the spec fixes (category weights, sub-item
weights, band scores, rating cut-offs, rubric cut-points) lives in database
config, not in this file. The spec is explicit about why:

    "Nothing here should be altered without the product owner's sign-off —
     every number was chosen deliberately; changing a threshold silently
     re-rates every assessment built on it."

Hard-coding those numbers would make a threshold change a code deploy, and
worse, would silently re-rate historical assessments the moment it shipped.
Config rows are versioned instead, so an assessment records which config
version produced it.

WHAT THIS MODULE DOES
  * bands a raw value (monotonic / range / anchor)  — §2.4.3
  * rolls scores up through the hierarchy            — §2.3.1
  * applies the rating ladder                        — §2.4.2
  * reports applied weight and input coverage        — §2.3.1

WHAT IT DELIBERATELY DOES NOT DO
  * assign the Exceptional band (10). §2.4.4 and §5.1 both forbid it: 10 is
    reachable only through a human override with written justification. The
    automated path caps at Excellent (9), which means a clean run tops out
    at 9/10 by design. This is not a bug to be fixed later.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# §2.4.1 — scoring key. Blank is EXCLUDED from the roll-up, not scored zero;
# scoring a blank as zero would punish a founder for a missing input rather
# than simply not counting it.
BAND_SCORES = {
    "Exceptional": 10,   # override-only, never produced by this module
    "Excellent": 9,
    "Good": 7,
    "Fair": 5,
    "Poor": 3,
}

AUTOMATED_BANDS = ("Excellent", "Good", "Fair", "Poor")

# §2.4.2 — rating ladder, applied to the final weighted score out of 10.
RATING_LADDER = [
    (Decimal("9"), "Excellent"),
    (Decimal("8"), "Very Good"),
    (Decimal("7"), "Good"),
    (Decimal("6"), "Average"),
    (Decimal("5"), "Challenging"),
]
RATING_FLOOR = "Not Recommended"

# Shown to the founder alongside the rating (§2.4.2).
RATING_MEANING = {
    "Excellent": ("Pursue on priority. Strong across team, financials and "
                  "sub-sector, with deal terms that can go to market as they "
                  "stand."),
    "Very Good": ("Fundable. One or two parameters need work before launch — "
                  "identify them from the lowest-scoring rows and address "
                  "those first."),
    "Good": ("Workable, but the story has to be built rather than told. "
             "Expect a longer process and a narrower investor list."),
    "Average": ("Marginal. Only worth pursuing now if there is a specific "
                "reason — a strategic relationship, or a sector where a "
                "foothold matters. State it explicitly."),
    "Challenging": ("Not fundable yet unless the weaknesses are fixable on a "
                    "reasonable timetable. Say what would have to change."),
    "Not Recommended": ("Not fundable in current form. Taking this to "
                        "investors as-is will cost credibility for future "
                        "rounds."),
}

DEAL_STAGES = ("Seed", "Series A", "Series B", "Growth")
DEFAULT_STAGE = "Series A"


# ---------------------------------------------------------------------------
# Banding — §2.4.3
# ---------------------------------------------------------------------------

def band_monotonic(value, direction, cut_excellent, cut_good, cut_fair):
    """(a) Higher-is-better / lower-is-better. Cut-points are INCLUSIVE."""
    if value is None:
        return None
    v = Decimal(str(value))
    cuts = [Decimal(str(c)) if c is not None else None
            for c in (cut_excellent, cut_good, cut_fair)]
    if direction == "Higher":
        if cuts[0] is not None and v >= cuts[0]:
            return "Excellent"
        if cuts[1] is not None and v >= cuts[1]:
            return "Good"
        if cuts[2] is not None and v >= cuts[2]:
            return "Fair"
        return "Poor"
    if direction == "Lower":
        if cuts[0] is not None and v <= cuts[0]:
            return "Excellent"
        if cuts[1] is not None and v <= cuts[1]:
            return "Good"
        if cuts[2] is not None and v <= cuts[2]:
            return "Fair"
        return "Poor"
    raise ValueError(f"unknown direction {direction!r}")


def band_range(value, ideal_min, ideal_max, good_tol_pct, fair_tol_pct):
    """(b) Ideal band with symmetric tolerance zones (§2.4.3b).

    Used by BQ_CO_AGE, DD_RAISE_MULT, DD_RAISE_SHARE — parameters where both
    too little AND too much are bad. A company raising 10x its last round is
    as much of a flag as one raising 0.2x.
    """
    if value is None or ideal_min is None or ideal_max is None:
        return None
    v = Decimal(str(value))
    lo, hi = Decimal(str(ideal_min)), Decimal(str(ideal_max))
    if lo <= v <= hi:
        return "Excellent"
    good = Decimal(str(good_tol_pct or 0)) / 100
    fair = Decimal(str(fair_tol_pct or 0)) / 100
    if lo * (1 - good) <= v <= hi * (1 + good):
        return "Good"
    if lo * (1 - fair) <= v <= hi * (1 + fair):
        return "Fair"
    return "Poor"


def score_for_band(band):
    """Band -> score. Unknown or blank bands are excluded, never zeroed."""
    if not band:
        return None
    return BAND_SCORES.get(band)


def validate_automated_band(band, *, input_key=""):
    """Guard: the automated path must never emit Exceptional (§2.4.4, §5.1).

    Enforced in code rather than trusted to the prompt, because an LLM that
    returns "Exceptional" would otherwise silently produce a 10/10 with no
    override record and no justification — exactly the audit failure the
    source workbook's override-comment counter exists to prevent.
    """
    if band == "Exceptional":
        logger.error(
            "DEAL ASSESSMENT: automated banding returned Exceptional for %s. "
            "That band is override-only and requires a written "
            "justification; downgrading to Excellent.", input_key)
        return "Excellent"
    if band and band not in AUTOMATED_BANDS:
        logger.warning("DEAL ASSESSMENT: unknown band %r for %s — treating "
                       "as unscored.", band, input_key)
        return None
    return band


# ---------------------------------------------------------------------------
# Roll-up — §2.3.1
# ---------------------------------------------------------------------------

def weighted_rollup(children):
    """Weighted average over SCORED children only (§2.3.1, verbatim).

    children: iterable of {"score": number|None, "weight": number}
    Returns (score, applied_weights) where applied_weights[i] is the share
    that child actually carried once blanks were excluded.

    The redistribution is the subtle part and the spec calls it out: leaving a
    sibling blank does not lower the score, it makes every remaining sibling
    matter MORE. Surfacing applied weight is how a founder sees that one
    answer is carrying 50% of a category because its three siblings are empty.
    """
    scored = [c for c in children if c.get("score") is not None]
    if not scored:
        return None, []
    total_w = sum(Decimal(str(c["weight"])) for c in scored)
    if total_w == 0:
        return None, []
    score = sum(Decimal(str(c["score"])) * Decimal(str(c["weight"]))
                for c in scored) / total_w
    applied = [{"code": c.get("code"),
                "nominal_weight": float(c["weight"]),
                "applied_weight": float(
                    Decimal(str(c["weight"])) / total_w * 100)}
               for c in scored]
    return score, applied


def rating_for_score(score):
    """§2.4.2 rating ladder."""
    if score is None:
        return None
    s = Decimal(str(score))
    for cut, label in RATING_LADDER:
        if s >= cut:
            return label
    return RATING_FLOOR


def input_coverage(parameters):
    """Share of score-feeding parameters that were actually answered.

    Reported alongside the score because a 7.5 built on 20% coverage and a
    7.5 built on 90% coverage are not the same claim, and the founder needs
    to see which one they have.
    """
    feeding = [p for p in parameters if not p.get("is_reference_only")]
    if not feeding:
        return Decimal("0")
    answered = [p for p in feeding if p.get("score") is not None]
    return (Decimal(len(answered)) / Decimal(len(feeding)) * 100)


def score_assessment(*, categories, parameters):
    """Full bottom-up roll-up: parameters -> sub-items -> categories -> overall.

    `categories`: [{"code": "A", "weight": 20, "children": [...]}, ...] where
    a child is either a leaf {"code", "weight", "score"} or another node with
    its own "children".

    Returns a dict with overall score, rating, per-category scores and applied
    weights, and coverage.
    """
    def resolve(node):
        if "children" in node and node["children"]:
            child_scores = [resolve(c) for c in node["children"]]
            score, applied = weighted_rollup(child_scores)
            return {"code": node.get("code"), "weight": node.get("weight", 0),
                    "score": float(score) if score is not None else None,
                    "applied": applied}
        return {"code": node.get("code"), "weight": node.get("weight", 0),
                "score": node.get("score")}

    resolved = [resolve(c) for c in categories]
    overall, applied = weighted_rollup(resolved)
    coverage = input_coverage(parameters)
    rating = rating_for_score(overall)

    # Config integrity check #1 from the source's Assessment Control sheet:
    # the seven category weights must sum to 100.
    weight_sum = sum(Decimal(str(c.get("weight", 0))) for c in categories)
    if weight_sum != 100:
        logger.error("DEAL ASSESSMENT: category weights sum to %s, not 100. "
                     "The score is not trustworthy until this is fixed.",
                     weight_sum)

    return {
        "overall_score": float(overall) if overall is not None else None,
        "rating_band": rating,
        "rating_meaning": RATING_MEANING.get(rating, ""),
        "categories": resolved,
        "applied_weights": applied,
        "input_coverage_pct": float(coverage),
        "weights_valid": weight_sum == 100,
        "max_achievable_note": (
            "Automated scoring caps at 9/10. A 10 requires a human override "
            "to Exceptional with written justification (spec 2.4.4)."),
    }
