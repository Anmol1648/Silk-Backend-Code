"""
Resolution helpers over the admin-owned configuration.

Every function degrades safely: if the admin has not configured a value,
a sensible shipped default is returned rather than an exception. That
keeps a fresh install working and means an admin mistake narrows
behaviour rather than breaking it.
"""
import logging
from decimal import Decimal

logger = logging.getLogger(__name__)

# Shipped fallbacks — used only when the DB has nothing.
DEFAULT_LOGO_URL = "/brand/silk-logo.svg"
DEFAULT_LOGO_MONO_URL = "/brand/silk-logo-white.svg"

_BRAND_FALLBACK = {
    "productName": "Silk",
    "browserTitle": "Silk — AI Dealmaking Workspace",
    "tagline": "",
    # The product marks ship with the web build and are served from its
    # origin, so branding works out of the box. An administrator can
    # override these with hosted URLs at any time.
    "logoUrl": DEFAULT_LOGO_URL,
    "logoMonoUrl": DEFAULT_LOGO_MONO_URL,
    "faviconUrl": DEFAULT_LOGO_URL,
    "primaryColor": "#1F6F4A",
    "supportEmail": "",
    "exportFooterText": "",
}

_STAGE_FALLBACK = [
    (0, "foundation", "Foundation", True),
    (1, "company_profile", "Company Profile", True),
    (2, "fundraising_strategy", "Fundraising Strategy", True),
    (3, "investor_discovery", "Investor Discovery", False),
    (4, "outreach", "Outreach", False),
    (5, "term_sheets", "Term Sheets", False),
    (6, "due_diligence", "Due Diligence", False),
    (7, "definitive_documents", "Definitive Documents", False),
    (8, "closing", "Closing", False),
]


# --------------------------------------------------------------------------
# Branding
# --------------------------------------------------------------------------
def brand():
    """Brand block for the API and export renderers."""
    try:
        from fundos.platformcfg.models import BrandConfig
        row = BrandConfig.get_solo()
        if not row:
            return dict(_BRAND_FALLBACK)
        return {
            "productName": row.product_name or "Silk",
            "legalName": row.legal_name,
            "tagline": row.tagline,
            "browserTitle": row.browser_title or f"{row.product_name}",
            "logoUrl": row.logo_url or DEFAULT_LOGO_URL,
            "logoMonoUrl": row.logo_mono_url or DEFAULT_LOGO_MONO_URL,
            "faviconUrl": row.favicon_url or DEFAULT_LOGO_URL,
            "exportLogoUrl": row.export_logo_url,
            "exportFooterText": row.export_footer_text,
            "supportEmail": row.support_email,
            "primaryColor": row.primary_color or "#1F6F4A",
        }
    except Exception:
        return dict(_BRAND_FALLBACK)


def product_name():
    return brand().get("productName", "Silk")


# --------------------------------------------------------------------------
# Stages (CR-03) — advisory prerequisites only (C6)
# --------------------------------------------------------------------------
def stages():
    """Ordered stage registry. Falls back to the shipped journey."""
    try:
        from fundos.platformcfg.models import StageDefinition
        rows = list(StageDefinition.objects.filter(is_active=True)
                    .order_by("stage_no"))
        if rows:
            return [{
                "stageNo": r.stage_no,
                "stageKey": r.stage_key,
                "label": r.label,
                "purpose": r.purpose,
                "overview": r.overview,
                "prerequisites": r.prerequisites or [],
                "nextActions": r.next_actions or [],
                "isImplemented": r.is_implemented,
            } for r in rows]
    except Exception:
        pass
    return [{
        "stageNo": n, "stageKey": k, "label": label,
        "purpose": "", "overview": "", "prerequisites": [], "nextActions": [],
        "isImplemented": impl,
    } for n, k, label, impl in _STAGE_FALLBACK]


def stage_numbers():
    return [s["stageNo"] for s in stages()]


def stage_label(stage_no):
    for s in stages():
        if s["stageNo"] == stage_no:
            return s["label"]
    return f"Stage {stage_no}"


# --------------------------------------------------------------------------
# Profile sections (C2 / CR-07)
# --------------------------------------------------------------------------
def profile_sections(active_only=True):
    try:
        from fundos.platformcfg.models import ProfileSectionConfig
        qs = ProfileSectionConfig.objects.all()
        if active_only:
            qs = qs.filter(is_active=True)
        return list(qs.order_by("sort_order"))
    except Exception:
        return []


def completeness_section_keys():
    """Sections that count toward a 'complete' Company Profile (C2)."""
    return [s.section_key for s in profile_sections()
            if s.required_for_completeness]


def creation_section_keys():
    return [s.section_key for s in profile_sections()
            if s.required_for_creation]


# --------------------------------------------------------------------------
# Fund-raise buckets & traction rules (C9)
# --------------------------------------------------------------------------
def buckets():
    try:
        from fundos.platformcfg.models import FundraiseBucket
        return list(FundraiseBucket.objects.filter(is_active=True)
                    .order_by("bucket_no"))
    except Exception:
        return []


def bucket_for_amount(amount_usd):
    """Nearest bucket at or above the amount; the top bucket beyond that."""
    rows = buckets()
    if not rows or amount_usd is None:
        return None
    for row in rows:
        if Decimal(str(amount_usd)) <= row.amount_usd:
            return row
    return rows[-1]


def _condition_matches(conditions, signals):
    """Evaluate one traction rule's conditions against company signals."""
    if not conditions:
        return False

    def num(key):
        val = signals.get(key)
        try:
            return float(val) if val is not None else None
        except (TypeError, ValueError):
            return None

    checks = (
        ("arr_usd_min", lambda v: (num("arr_usd") or 0) >= v),
        ("arr_usd_max", lambda v: (num("arr_usd") or 0) < v),
        ("capital_raised_usd_min", lambda v: (num("capital_raised_usd") or 0) >= v),
        ("capital_raised_usd_max", lambda v: (num("capital_raised_usd") or 0) < v),
        ("paying_customers_min", lambda v: (num("paying_customers") or 0) >= v),
        ("months_full_time_min", lambda v: (num("months_full_time") or 0) >= v),
        ("founder_full_time", lambda v: bool(signals.get("founder_full_time")) is bool(v)),
        ("company_registered", lambda v: bool(signals.get("company_registered")) is bool(v)),
    )
    for key, test in checks:
        if key in conditions:
            try:
                if not test(conditions[key]):
                    return False
            except Exception:
                return False
    return True


def recommend_bucket(signals):
    """Select the default bucket from company traction signals (C9).

    Returns {bucket, rule, uplift_applied, rationale, advice} or None.
    The founder may override with any other bucket — this is a default,
    not a constraint.
    """
    rows = buckets()
    if not rows:
        return None

    try:
        from fundos.platformcfg.models import TractionRule, UpliftRule
        rules = list(TractionRule.objects.filter(is_active=True)
                     .select_related("recommended_bucket").order_by("priority"))
    except Exception:
        rules = []

    matched = None
    for rule in rules:
        if _condition_matches(rule.conditions or {}, signals):
            matched = rule
            break

    insufficient = False
    missing = []
    if matched:
        bucket = matched.recommended_bucket
        rationale = matched.rationale
        advice = matched.advice_note
    else:
        # No rule matched. That is not the same as "raise the smallest
        # amount" — it means we do not know enough about the company yet.
        # Presenting bucket 1 as a recommendation here reads as advice and
        # is wrong for, say, a company with revenue whose profile is thin.
        # Return the smallest bucket only as a placeholder, clearly flagged,
        # and name what would produce a real recommendation.
        bucket = rows[0]
        insufficient = True
        if not signals.get("arr_usd"):
            missing.append("annual recurring revenue")
        if not signals.get("capital_raised_usd"):
            missing.append("capital raised to date")
        if not signals.get("founder_full_time"):
            missing.append("whether a founder is full-time")
        rationale = (
            "There isn't enough information yet to recommend a target. "
            + ("Add " + ", ".join(missing) + " to get a recommendation."
               if missing else
               "Complete the company profile to get a recommendation."))
        advice = ""

    # Uplift (Outline §3.3g) — star founder / exceptional sector.
    uplift_applied = []
    try:
        from fundos.platformcfg.models import UpliftRule as _U
        # An uplift on a placeholder would compound a guess with a bonus.
        for up in ([] if insufficient else _U.objects.filter(is_active=True)):
            if signals.get(up.condition_key):
                idx = next((i for i, b in enumerate(rows)
                            if b.bucket_no == bucket.bucket_no), 0)
                new_idx = min(idx + up.levels, len(rows) - 1)
                if new_idx != idx:
                    bucket = rows[new_idx]
                    uplift_applied.append({
                        "name": up.name, "levels": up.levels,
                        "rationale": up.rationale,
                    })
    except Exception:
        pass

    return {
        "bucket": bucket,
        "rule": matched,
        "uplift_applied": uplift_applied,
        "rationale": rationale,
        "advice": advice,
        # True when no rule matched: `bucket` is a placeholder for display,
        # not a recommendation. Clients must not present it as advice.
        "insufficient_data": insufficient,
        "missing_signals": missing,
    }


# --------------------------------------------------------------------------
# FX (C11)
# --------------------------------------------------------------------------
DEFAULT_USD_INR = Decimal("84.0")


def fx_rate(base="USD", quote="INR"):
    """Latest dated rate. Returns (rate, as_of_date, source)."""
    if base == quote:
        return Decimal("1"), None, "identity"
    try:
        from fundos.platformcfg.models import FxRate
        row = FxRate.latest(base, quote)
        if row:
            return row.rate, row.as_of_date, row.source
        inverse = FxRate.latest(quote, base)
        if inverse and inverse.rate:
            return (Decimal("1") / inverse.rate, inverse.as_of_date,
                    f"{inverse.source} (inverted)")
    except Exception:
        pass
    if (base, quote) == ("USD", "INR"):
        return DEFAULT_USD_INR, None, "default"
    if (base, quote) == ("INR", "USD"):
        return Decimal("1") / DEFAULT_USD_INR, None, "default"
    return Decimal("1"), None, "unavailable"


def to_usd(amount, ccy):
    """Convert to USD, returning (value, rate_used, as_of, source)."""
    if amount is None:
        return None, None, None, None
    ccy = (ccy or "USD").upper()
    if ccy == "USD":
        return Decimal(str(amount)), Decimal("1"), None, "identity"
    rate, as_of, source = fx_rate("USD", ccy)
    if not rate:
        return None, None, None, "unavailable"
    return Decimal(str(amount)) / rate, rate, as_of, source


def home_currency_for_country(iso2):
    try:
        from fundos.platformcfg.models import Country
        row = Country.objects.filter(iso2=(iso2 or "").upper()).first()
        if row and row.home_currency:
            return row.home_currency
    except Exception:
        pass
    return "USD"


# --------------------------------------------------------------------------
# UI copy
# --------------------------------------------------------------------------
def copy(key, default=""):
    try:
        from fundos.platformcfg.models import UiCopy
        return UiCopy.get(key, default)
    except Exception:
        return default
