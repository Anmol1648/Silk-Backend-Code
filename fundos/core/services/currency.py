"""
Currency presentation (Gap G13).

The deterministic engines normalise and compute in USD (CKB values are INR
for the India market and are divided by the USD/INR rate on the way in).
Persistence stays in USD so engine maths and history remain stable; the
PRESENTATION EDGE converts to a single presentation currency (INR by
default for the India market) and always exposes the FX rate used as an
explicit assumption — keeping the (value, ccy, basis) triple honest.

Settings:
  FUNDOS_USD_INR              indicative USD/INR rate (default 84.0)
  FUNDOS_PRESENTATION_CCY     "INR" (default) or "USD" (passthrough)
"""
from django.conf import settings


def usd_inr_rate() -> float:
    return float(getattr(settings, "FUNDOS_USD_INR", 84.0))


def presentation_ccy() -> str:
    return str(getattr(settings, "FUNDOS_PRESENTATION_CCY", "INR")).upper()


def present_money(usd_value, basis: str = "") -> dict:
    """Convert a stored USD amount to the presentation currency.

    Returns the (value, ccy, basis) triple plus the underlying USD value so
    the frontend can show either without re-deriving the rate.
    """
    if usd_value is None:
        out = {"value": None, "ccy": presentation_ccy()}
    elif presentation_ccy() == "USD":
        out = {"value": round(float(usd_value), 2), "ccy": "USD"}
    else:
        out = {"value": round(float(usd_value) * usd_inr_rate(), 2),
               "ccy": "INR", "usdValue": round(float(usd_value), 2)}
    if basis:
        out["basis"] = basis
    return out


def fx_block() -> dict:
    """Attached to money-bearing responses — the rate is an assumption."""
    return {"usdInr": usd_inr_rate(), "presentationCcy": presentation_ccy(),
            "note": "Indicative conversion rate — engines compute in USD; "
                    "amounts are presented in the configured currency."}


def to_usd(value, ccy: str = "") -> float:
    """Normalise an input amount to USD for the engines."""
    v = float(value)
    if (ccy or presentation_ccy()).upper() == "INR":
        return v / usd_inr_rate()
    return v
