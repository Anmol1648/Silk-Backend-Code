"""Parameters that are arithmetic, computed instead of read.

Some scorecard rows are not facts anybody wrote down. "Raise vs Last Round"
is the current ask divided by the previous round; "Raise vs Total Raised" is
the ask over everything raised to date. Nothing in a deck states either. A
model asked for them does the division itself, and the row then arrives
labelled

    Estimate — extractor confidence: high
    This row rests on that confidence rather than on the strength of its
    source.

which is the wrong claim twice over. The division is certain. What is
uncertain is the two INPUTS, and those are extracted separately and already
carry their own tiers.

Left to the model, three things go wrong, all of them observed on real runs:

1. **It converts currency to divide.** One Tyreplex run stated both answers
   in the same sentence — "INR 20 crore (~$2.4M) and ... INR 12 crore
   (~$1.6M) ... = 1.5x. (20 Cr / 12 Cr = 1.66x)" — and stored 1.5x. 12 crore
   is $1.45M, not $1.6M; rounding to one decimal before dividing produced a
   figure 10% low, in a ratio where the currency cancels out. The stored 1.5
   then landed exactly on the ideal_min of 1.5 and scored Excellent by sitting
   on the boundary.

2. **It picks the wrong "current round".** Both rows divided using the Series
   A that CLOSED in January 2025 rather than the $10M being raised now. Right
   answers: 4.2x and 164%. Stored: 1.5x and 39.3%, both Excellent, where the
   truth is Fair and below-Fair. Two of the highest-scoring rows on the
   scorecard were flattering the deal.

3. **It cites its own working.** The `quote` on one of those rows was four
   hundred words of the model reasoning aloud through five candidate answers,
   stored as a verbatim extract from the dossier.

Computed here, in the source currency, from inputs each carrying its own
citation, none of those failures is reachable. The tier of the result is the
WEAKEST tier among its inputs, because a ratio can be no better evidenced
than the numbers it divides.

This module decides no thresholds and no bands. `deal_assessment` scores the
value exactly as it scores an extracted one.
"""
import logging
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

#: Current ask / previous round, as a multiple.
RAISE_MULTIPLE = "DD_RAISE_MULT"

#: Current ask / everything raised to date, as a percentage.
RAISE_SHARE = "DD_RAISE_SHARE"

DERIVED_KEYS = (RAISE_MULTIPLE, RAISE_SHARE)


def _number(value):
    """A Decimal, or None. Never raises on whatever the caller has."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return out if out.is_finite() else None


def _round_date(row):
    """A sortable date for a funding round. Missing sorts oldest."""
    text = str((row or {}).get("date") or "").strip()
    # "2022-08" is a real value in this data and must not sort as "2022-08-01"
    # would be wrong; padding to the first of the month orders it correctly
    # against full dates without inventing precision anywhere it is read.
    if len(text) == 7:
        text = f"{text}-01"
    return text or "0000-00-00"


def previous_round(rounds):
    """The most recent CLOSED round that states an amount.

    Rounds with no amount are skipped rather than treated as zero: three of
    Tyreplex's five rounds have `amount_usd_mn: null`, and a null denominator
    is a missing input, not a small one.
    """
    dated = [r for r in (rounds or [])
             if isinstance(r, dict) and _number(r.get("amount_usd_mn")) is not None]
    if not dated:
        return None
    return max(dated, key=_round_date)


def total_raised(rounds, reported=None):
    """Everything raised to date, and how sure we are of it.

    :returns: ``(amount, basis)`` — basis is "rounds" when every round states
        an amount and they were summed, "reported" when the figure came from
        outside because the table could not produce one, or "" when neither.

    The distinction is the point. Tyreplex's funding table accounts for $4.0M
    across two of five rounds while the reported total is $6.1M from Tracxn:
    summing the table would understate the denominator by a third, and using
    the reported figure without saying so hides that a third of it is
    unevidenced.
    """
    rows = [r for r in (rounds or []) if isinstance(r, dict)]
    amounts = [_number(r.get("amount_usd_mn")) for r in rows]
    if rows and all(a is not None for a in amounts):
        return sum(amounts), "rounds"
    stated = _number(reported)
    if stated is not None and stated > 0:
        return stated, "reported"
    return None, ""


def raise_multiple(current_raise, rounds):
    """Current ask / previous round.

    :returns: ``(value, working)`` or ``(None, working)`` when an input is
        missing — never a guess, and `working` always says which one.
    """
    ask = _number(current_raise)
    prior = previous_round(rounds)
    prior_amount = _number((prior or {}).get("amount_usd_mn"))

    working = {"current_raise": float(ask) if ask is not None else None,
               "previous_round": (prior or {}).get("round") or "",
               "previous_round_date": (prior or {}).get("date") or "",
               "previous_amount": (float(prior_amount)
                                   if prior_amount is not None else None)}
    if ask is None:
        working["missing"] = "the amount being raised is not recorded"
        return None, working
    if prior_amount is None or prior_amount <= 0:
        working["missing"] = "no previous round states an amount"
        return None, working

    value = ask / prior_amount
    working["formula"] = (f"{ask} / {prior_amount} = "
                          f"{value.quantize(Decimal('0.01'))}")
    return float(round(value, 2)), working


def raise_share(current_raise, rounds, reported_total=None):
    """Current ask / total raised to date, as a percentage."""
    ask = _number(current_raise)
    total, basis = total_raised(rounds, reported_total)

    working = {"current_raise": float(ask) if ask is not None else None,
               "total_raised": float(total) if total is not None else None,
               "total_basis": basis}
    if ask is None:
        working["missing"] = "the amount being raised is not recorded"
        return None, working
    if total is None or total <= 0:
        working["missing"] = "total capital raised is not recorded"
        return None, working

    value = ask / total * 100
    working["formula"] = (f"{ask} / {total} x 100 = "
                          f"{value.quantize(Decimal('0.01'))}%")
    if basis == "reported":
        working["caveat"] = (
            "Total raised comes from a reported figure, not from the funding "
            "table, because at least one round states no amount.")
    return float(round(value, 2)), working


#: Evidence tiers, worst first. A derived row inherits the weakest of its
#: inputs: a ratio can be no better evidenced than the numbers it divides.
_TIER_ORDER = (3, 2, 1)


def weakest_tier(*tiers):
    """The least trustworthy tier among the inputs, or None if none given."""
    known = [t for t in tiers if isinstance(t, int) and t in _TIER_ORDER]
    return max(known) if known else None


def build_rows(*, current_raise, rounds, reported_total, input_tiers=None,
               citations=None):
    """The derived rows, ready for the same persistence as extracted ones.

    :param input_tiers: ``{input name: tier}`` for the terms, so the result
        can inherit the weakest rather than claiming one of its own.
    :param citations: ``{input name: {source, locator, quote}}`` — the terms
        keep the citations they arrived with, so a reader can check one term
        without re-deriving the whole figure.
    :returns: A list of row dicts. A parameter whose inputs are missing is
        OMITTED, not written as zero — a blank row is excluded from the
        roll-up and redistributes its weight, which is the honest outcome.
    """
    tiers = input_tiers or {}
    cites = citations or {}
    rows = []

    multiple, working = raise_multiple(current_raise, rounds)
    if multiple is not None:
        rows.append(_row(RAISE_MULTIPLE, multiple, "x", working,
                         weakest_tier(tiers.get("current_raise"),
                                      tiers.get("previous_round")),
                         [cites.get("current_raise"),
                          cites.get("previous_round")]))
    else:
        logger.info("DERIVED: %s not computed — %s",
                    RAISE_MULTIPLE, working.get("missing"))

    share, working = raise_share(current_raise, rounds, reported_total)
    if share is not None:
        rows.append(_row(RAISE_SHARE, share, "%", working,
                         weakest_tier(tiers.get("current_raise"),
                                      tiers.get("total_raised")),
                         [cites.get("current_raise"),
                          cites.get("total_raised")]))
    else:
        logger.info("DERIVED: %s not computed — %s",
                    RAISE_SHARE, working.get("missing"))
    return rows


def _row(input_key, value, unit, working, tier, citations):
    """One derived row in the shape the extracted ones use."""
    terms = [c for c in citations if isinstance(c, dict) and c.get("source")]
    detail = " | ".join(
        f"{c['source']}{(' · ' + c['locator']) if c.get('locator') else ''}"
        for c in terms)
    justification = working.get("formula", "")
    if working.get("caveat"):
        justification = f"{justification} {working['caveat']}"

    return {
        "input_key": input_key,
        "value": str(value)[:255],
        "unit": unit,
        "source_type": "derived",
        "source_url": "",
        "source_detail": detail[:255] or "computed from recorded inputs",
        "source_tier": tier or 3,
        # Certainty about the arithmetic, not about the terms — the terms
        # carry their own tiers above, which is where doubt belongs.
        "confidence": 1.0,
        "justification": justification[:2000],
    }
