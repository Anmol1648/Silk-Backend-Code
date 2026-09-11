"""An amount, the currency it was stated in, and the scale it was stated at.

A number on its own is not a fact about money. "20" is ₹20 crore or $20
million or 20 rupees depending on two labels that have to travel with it, and
when they don't, something downstream supplies them by guessing.

That is not hypothetical. `funding_history.amount_usd_mn` can hold only USD
millions, so a round the deck states as ₹20 crore has to be converted before
it can be written at all — by the model, at a rate it decided in a sentence
("using an approximate exchange rate of 83 INR to 1 USD") and then discarded.
What reached the database was `2.4`. The deck's own words were gone, the rate
was unrecorded and unauditable, and one crore figure was rounded to $1.6M
when it is $1.45M, which put a scorecard row 10% low.

`financial_summary` shows the other half of the same failure: the model
converted ₹56.9 crore to $6.828M and then labelled the result `"currency":
"INR"`. Read as stated, FY2031's 415.8 means ₹415.8 million — about $5M. It
means $415.8M. An 83x misreading on the row a reader cares most about.

So: the reported figure is the fact and is stored as reported. The USD figure
is a convenience, computed from a rate that is recorded with its date, and
labelled as derived. A wrong rate then becomes a correctable error rather than
a permanent one, which is the whole difference.
"""
import logging
import re
from decimal import Decimal, InvalidOperation

logger = logging.getLogger(__name__)

#: Scale as written, and what one unit of it is worth. Crore and lakh are
#: here because ₹20 crore is how Indian founders, decks and press state an
#: amount; normalising it to 200,000,000 at storage makes every Indian figure
#: unrecognisable to the person who wrote it.
DENOMINATIONS = {
    "": Decimal(1),
    "K": Decimal(1_000),
    "L": Decimal(100_000),
    "Mn": Decimal(1_000_000),
    "Cr": Decimal(10_000_000),
    "Bn": Decimal(1_000_000_000),
}

#: What people actually type, mapped to the canonical form above.
_DENOMINATION_WORDS = {
    "k": "K", "thousand": "K", "thousands": "K",
    "l": "L", "lac": "L", "lacs": "L", "lakh": "L", "lakhs": "L",
    "m": "Mn", "mn": "Mn", "mm": "Mn", "million": "Mn", "millions": "Mn",
    "cr": "Cr", "crore": "Cr", "crores": "Cr", "cro": "Cr",
    "b": "Bn", "bn": "Bn", "billion": "Bn", "billions": "Bn",
}

_CURRENCY_WORDS = {
    "₹": "INR", "rs": "INR", "rs.": "INR", "inr": "INR", "rupee": "INR",
    "rupees": "INR",
    "$": "USD", "us$": "USD", "usd": "USD", "dollar": "USD",
    "dollars": "USD", "us dollars": "USD",
    "€": "EUR", "eur": "EUR", "euro": "EUR", "euros": "EUR",
    "£": "GBP", "gbp": "GBP", "pound": "GBP", "pounds": "GBP",
}

_SYMBOLS = {"INR": "₹", "USD": "US$", "EUR": "€", "GBP": "£"}

#: "₹20 crore", "US$ 6.3Mn", "INR 56.9 Cr", "$2.4 million"
_MONEY = re.compile(
    r"(?P<currency>₹|\$|€|£|US\$|Rs\.?|INR|USD|EUR|GBP)?\s*"
    r"(?P<amount>-?[\d,]+(?:\.\d+)?)\s*"
    r"(?P<denomination>[A-Za-z]{1,9}\.?)?",
    re.IGNORECASE)


def normalise_currency(text, default=""):
    """An ISO code from whatever the document wrote, or `default`."""
    key = str(text or "").strip().lower().rstrip(".")
    if not key:
        return default
    if key.upper() in _SYMBOLS:
        return key.upper()
    return _CURRENCY_WORDS.get(key, default)


def normalise_denomination(text):
    """A canonical scale from whatever the document wrote, or ""."""
    key = str(text or "").strip().lower().rstrip(".")
    if not key:
        return ""
    if key in ("cr", "l", "k", "mn", "bn"):
        return _DENOMINATION_WORDS[key]
    return _DENOMINATION_WORDS.get(key, "")


def _decimal(value):
    """A Decimal, or None. Never raises on whatever the caller has."""
    if value is None or isinstance(value, bool):
        return None
    try:
        out = Decimal(str(value).strip().replace(",", ""))
    except (InvalidOperation, ValueError, AttributeError):
        return None
    return out if out.is_finite() else None


def parse(text, default_currency=""):
    """Read "₹20 crore" as ``(20, "INR", "Cr")``.

    :returns: ``(amount, currency, denomination)``, any of which may be None
        or "" when the text does not say. Nothing is inferred: a bare "20"
        comes back with no currency and no scale, because that is what it is.
    """
    if text is None:
        return None, default_currency, ""
    match = _MONEY.search(str(text))
    if not match:
        return None, default_currency, ""
    amount = _decimal(match.group("amount"))
    currency = normalise_currency(match.group("currency"), default_currency)
    denomination = normalise_denomination(match.group("denomination"))
    return amount, currency, denomination


def units(amount, denomination):
    """The amount in whole units of its currency. ₹20 Cr -> 200,000,000."""
    value = _decimal(amount)
    if value is None:
        return None
    return value * DENOMINATIONS.get(normalise_denomination(denomination)
                                     or denomination or "", Decimal(1))


def to_usd_mn(amount, currency, denomination, rate=None):
    """The amount in USD millions, and the rate used to get there.

    :param rate: units of `currency` per USD. Omitted, the configured USD/INR
        rate is used for INR and nothing else is converted.
    :returns: ``(value, rate_used)``. ``(None, None)`` when the amount is
        missing or the currency cannot be converted — a figure we cannot
        convert is left BLANK rather than passed through as if it were
        already USD, which is how ₹56.9 crore came to be stored as 6.828 and
        labelled INR.
    """
    whole = units(amount, denomination)
    if whole is None:
        return None, None

    code = (normalise_currency(currency) or currency or "").upper()
    if code == "USD":
        return float(round(whole / DENOMINATIONS["Mn"], 4)), None

    used = _decimal(rate)
    if used is None and code == "INR":
        from fundos.core.services.currency import usd_inr_rate
        used = _decimal(usd_inr_rate())
    if used is None or used <= 0:
        logger.info("MONEY: no rate available for %s — the USD companion is "
                    "left blank rather than guessed.", code or "an unnamed "
                    "currency")
        return None, None

    return float(round(whole / used / DENOMINATIONS["Mn"], 4)), float(used)


def display(amount, currency, denomination):
    """The amount as a reader would see it written: "₹20 Cr", "US$ 6.3Mn"."""
    value = _decimal(amount)
    if value is None:
        return ""
    code = (normalise_currency(currency) or currency or "").upper()
    symbol = _SYMBOLS.get(code, code)
    scale = normalise_denomination(denomination) or ""
    # 20 rather than 20.00; 6.3 rather than 6.30.
    text = f"{value.normalize():f}" if value == value.to_integral_value() \
        else f"{value.normalize():f}"
    joiner = " " if symbol and len(symbol) > 1 else ""
    return f"{symbol}{joiner}{text}{(' ' + scale) if scale else ''}".strip()


def block(amount, currency, denomination, *, rate=None, as_of=None):
    """The full money block: as reported, plus a labelled USD companion.

    The top three keys are the fact — what the document said. `amountUsdMn`
    is a convenience for comparison and sorting, and carries the rate and the
    date it was struck so a wrong rate is correctable rather than permanent.
    """
    value = _decimal(amount)
    code = normalise_currency(currency) or (currency or "").upper()
    scale = normalise_denomination(denomination) or ""
    usd, used = to_usd_mn(value, code, scale, rate)
    out = {
        "amount": float(value) if value is not None else None,
        "currency": code,
        "denomination": scale,
        "display": display(value, code, scale),
        "amountUsdMn": usd,
    }
    if usd is not None and used is not None:
        out["fxRate"] = used
        out["fxAsOf"] = str(as_of) if as_of else ""
        out["fxBasis"] = "derived"
    return out


def reported_usd_mn(amount, currency, denomination, *, derived=None,
                    rate=None):
    """The USD-millions companion for a figure stored as it was reported.

    Three columns predate the money fields and carried their scale in their
    NAME — `amount_usd_mn`, `revenue_m`, `pre_money_value`. A row written
    before the scale had anywhere to go states no denomination, and a blank
    read as whole units turns US$5 Mn into US$0.000005 Mn: the same defect
    the dashboard total had when it divided by a million a second time. So a
    blank scale here means MILLIONS, which is what those names always meant.

    A blank currency means USD for the same reason: the columns could hold
    nothing else, so that is what a row that never named one contains.

    :param derived: a USD companion already stored beside the figure. It was
        computed when the figure was written, at a rate recorded with it, so
        it is preferred over re-deriving one at today's rate.
    :returns: ``(value, rate_used)``; ``(None, None)`` when the figure is
        missing or its currency cannot be converted.
    """
    stored = _decimal(derived)
    if stored is not None:
        return float(stored), None
    return to_usd_mn(amount,
                     normalise_currency(currency) or (currency or "").upper()
                     or "USD",
                     normalise_denomination(denomination) or "Mn",
                     rate)
