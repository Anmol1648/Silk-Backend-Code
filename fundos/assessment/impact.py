"""What a change to one row would do to the score, computed not estimated.

"If this were settled, what happens to the score?" is the question a founder
actually wants answered, and it is the one a language model is worst at: it
has the weights in front of it and will still do the arithmetic in prose.

So the engine answers it. The same tree the scorer builds is rebuilt with one
leaf substituted and run through the same roll-up, which means the number
includes the part nobody does in their head -- blanks redistribute their
weight, so a row's real influence depends on how many of its siblings are
unanswered.

NOTHING HERE PERSISTS. A preview that wrote would be an override without a
reason, which is the one thing the override path exists to prevent.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)


def _clone(node):
    """A deep-enough copy that substitution cannot touch the stored tree."""
    out = dict(node)
    if node.get("children"):
        out["children"] = [_clone(c) for c in node["children"]]
    return out


def _substitute(nodes, changes, applied):
    """Write the proposed scores onto the cloned tree, in place."""
    for node in nodes:
        if node.get("children"):
            _substitute(node["children"], changes, applied)
            continue
        key = node.get("input_key") or node.get("code")
        if key in changes:
            node["score"] = changes[key]
            applied.add(key)


def _score(tree, assessment, tenant_id):
    from fundos.assessment.services import _coverage_population
    from fundos.engines.deal_assessment import score_assessment

    return score_assessment(
        categories=tree,
        parameters=_coverage_population(assessment, tenant_id))


def preview(assessment, changes):
    """``{overall, rating, categories}`` before and after ``changes``.

    :param changes: ``{input_key: score}``. A key with no leaf in the tree is
        reported in ``unknown`` rather than silently ignored -- a preview that
        quietly drops half its inputs reads as "no effect".
    """
    from fundos.assessment.services import _build_tree

    tenant_id = getattr(assessment, "tenant_id", None)
    clean = {}
    for key, value in (changes or {}).items():
        try:
            number = float(value)
        except (TypeError, ValueError):
            continue
        if 0 <= number <= 10:
            clean[key] = number

    tree = _build_tree(assessment, tenant_id)
    before = _score(tree, assessment, tenant_id)

    after_tree = [_clone(node) for node in tree]
    applied = set()
    _substitute(after_tree, clean, applied)
    after = _score(after_tree, assessment, tenant_id)

    by_code = {c["code"]: c for c in (before.get("categories") or [])}
    categories = []
    for row in after.get("categories") or []:
        was = (by_code.get(row["code"]) or {}).get("score")
        now = row.get("score")
        if was is None and now is None:
            continue
        if was != now:
            categories.append({"code": row["code"], "from": _round(was),
                               "to": _round(now), "delta": _delta(was, now)})

    return {
        # ROUNDED WHERE IT IS READ. A score is shown to two decimals
        # everywhere in this product, and handing a client 7.466666666666667
        # invites it to render that, or to round it differently from the
        # sentence sitting beside it.
        "overall": {"from": _round(before.get("overall_score")),
                    "to": _round(after.get("overall_score")),
                    "delta": _delta(before.get("overall_score"),
                                    after.get("overall_score"))},
        "rating": {"from": before.get("rating_band"),
                   "to": after.get("rating_band"),
                   "changed": before.get("rating_band")
                   != after.get("rating_band")},
        "categories": categories,
        "unknown": sorted(set(clean) - applied),
    }


def _round(value):
    """Two decimals, or None. The precision every score is displayed at."""
    if value is None:
        return None
    return round(float(value), 2)


def _delta(was, now):
    if was is None or now is None:
        return None
    return round(float(Decimal(str(now)) - Decimal(str(was))), 2)


def if_settled_at(assessment, input_key, band="Good"):
    """The preview for one row moved to a band, for a suggestion's own text.

    Returns ``(preview, band_score)``; ``(None, None)`` when the band has no
    score, so a caller shows nothing rather than a zero.
    """
    from fundos.engines.deal_assessment import score_for_band

    target = score_for_band(band)
    if target is None:
        return None, None
    return preview(assessment, {input_key: target}), target


def sentence(prev):
    """The preview as one line a person can read, or "" when it moves nothing.

    "the overall goes from 6.08 to 6.31 (+0.23)" -- and, when it crosses a
    ladder boundary, the fact that the RATING changes, which is the part a
    founder cares about far more than the decimal.
    """
    overall = (prev or {}).get("overall") or {}
    if overall.get("delta") in (None, 0, 0.0):
        return ""
    text = (f"the overall goes from {overall['from']:.2f} to "
            f"{overall['to']:.2f} ({overall['delta']:+.2f})")
    rating = (prev or {}).get("rating") or {}
    if rating.get("changed") and rating.get("to"):
        text += f", and the rating changes to {rating['to']}"
    return text
