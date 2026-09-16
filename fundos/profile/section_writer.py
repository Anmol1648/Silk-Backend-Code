"""
Unified section update for the standardized PATCH contract
(section-data-integrity-and-update-api.docx §3).

    PATCH /api/v1/companies/{companyId}/profile/sections/{sectionKey}
    body:  { "data": <section's data shape from Section 7> }

ONE contract for every section — the body is always ``{"data": ...}`` whether
that section's data is an object (company_profile, business_model,
financial_summary, ai_company_summary) or an array (founders, key_people,
products_services, customers_markets, competitive_advantages, revenue_model,
company_metrics, funding_history, competitors, recent_news). No per-section
special-casing at the API surface.

This is the WRITE side of the exact same field schema the serializer reads on
the GET side (fundos.profile.spec_serializer §7). The spec field names — e.g.
founders use ``role`` / ``background`` / ``is_full_time`` — are mapped back to
storage here, so a client can round-trip: read a section, edit ``data``, PATCH
it back verbatim, and read the same values again.

Storage per section:
  * object sections   → ProfileSection.structured (or content for
                        ai_company_summary's summary text)
  * structured lists  → ProfileSection.structured["items"]
  * entity-list       → full replacement of the section's own table rows
                        (founders, key_people, competitors, funding_history,
                        recent_news)

Every write recomputes needs_input and completeness so isComplete is never
left stale (fixes §2/§4).
"""
import logging
import re
from decimal import Decimal, InvalidOperation
from urllib.parse import urlparse

_logger = logging.getLogger("fundos.profile")


# Object-shaped sections and their allowed fields (spec §7).
#
# These lists must stay in step with what the serializer EMITS, or the
# round-trip the module docstring promises silently loses data: the client
# reads a section, PATCHes the same object back, and any key missing here is
# dropped on the floor without an error.
_OBJECT_FIELDS = {
    "company_profile": [
        "description_of_business", "website", "country", "macro_sector",
        "sub_sector", "funding_status_name", "employee_strength_name",
        "revenue_size_name", "currency_id",
        # Req 1 funding summary. The serializer prefers an editor-supplied
        # value over the derived one, so these have to be writable for that
        # preference to ever apply.
        "total_funding_raised_usd_mn", "last_funding_round_date",
        "latest_pre_money_usd_mn", "latest_post_money_usd_mn",
        # Req 2 display-format fields (CURRENCY:AMOUNT:SCALE).
        # Stored as-is; also parsed to update the corresponding _usd_mn field.
        "total_funding_raised_display", "latest_pre_money_display",
        "latest_post_money_display",
    ],
    "business_model": [
        "customer_type", "value_proposition", "delivery_model",
        "pricing_model", "sales_model", "distribution_channels",
        # Req 4 — emitted by the serializer, so it must be writable.
        "business_model_types",
    ],
}

# Object-section fields that are lists rather than scalars.
_OBJECT_LIST_FIELDS = {"distribution_channels", "business_model_types"}

# Object-section fields that are numeric.
_OBJECT_NUMERIC_FIELDS = {
    "total_funding_raised_usd_mn", "latest_pre_money_usd_mn",
    "latest_post_money_usd_mn",
}

# Allowed values for business_model_types, mirroring the serializer's filter.
_BUSINESS_MODEL_TYPES = {"B2B", "B2C", "B2B2C", "D2C", "SaaS", "Marketplace",
                         "Other"}

# --- Req 2: display-format monetary string parser ---
# Display strings arrive as "CURRENCY:AMOUNT:SCALE", e.g. "USD:10.00:Cr".
# We parse and convert to USD millions for the _usd_mn fields.

# Scale multipliers: value × multiplier = value in base units of that currency.
_SCALE_MULTIPLIERS = {
    "K": 1_000,
    "L": 100_000,           # Lakh
    "M": 1_000_000,         # Million
    "Cr": 10_000_000,       # Crore
    "B": 1_000_000_000,     # Billion
}

# Approximate FX rates to USD.  Used ONLY for the display→USD conversion when
# the founder picks a non-USD currency.  The real FX table in platformcfg.FxRate
# could be queried, but these display fields are advisory — an approximate
# conversion is better than dropping the figure.
_FX_TO_USD = {
    "USD": 1.0,
    "INR": 0.012,    # ~83 INR/USD
    "EUR": 1.08,
    "GBP": 1.27,
    "SGD": 0.74,
    "AED": 0.27,
    "JPY": 0.0067,
    "CAD": 0.74,
}

# Which display field drives which _usd_mn field.
_DISPLAY_TO_USD_MN = {
    "total_funding_raised_display": "total_funding_raised_usd_mn",
    "latest_pre_money_display": "latest_pre_money_usd_mn",
    "latest_post_money_display": "latest_post_money_usd_mn",
}


def _parse_display_to_usd_mn(display_str):
    """Parse 'CURRENCY:AMOUNT:SCALE' → USD millions (float), or None.

    Examples:
        'USD:10.00:Cr'  → 10 × 10,000,000 × 1.0  / 1,000,000 = 100.0
        'INR:420.84:M'  → 420.84 × 1,000,000 × 0.012 / 1,000,000 = 5.05
        'USD:5.01:M'    → 5.01 × 1,000,000 × 1.0 / 1,000,000 = 5.01
        ''              → None
    """
    if not display_str or not isinstance(display_str, str):
        return None
    parts = display_str.split(":")
    if len(parts) != 3:
        return None
    currency, amount_str, scale = parts[0].strip(), parts[1].strip(), parts[2].strip()
    try:
        amount = float(amount_str)
    except (ValueError, TypeError):
        return None
    multiplier = _SCALE_MULTIPLIERS.get(scale)
    if multiplier is None:
        return None
    fx = _FX_TO_USD.get(currency, None)
    if fx is None:
        return None
    # Convert to USD, then express in millions.
    usd_value = amount * multiplier * fx
    usd_mn = round(usd_value / 1_000_000, 4)
    return usd_mn

# Structured-list sections (stored in ProfileSection.structured["items"]) and
# the allowed keys for each row.
_STRUCTURED_LIST_FIELDS = {
    "products_services": ["name", "category", "description"],
    "customers_markets": ["market", "customer_type", "geography"],
    "competitive_advantages": ["title", "description"],
    "revenue_model": ["stream", "share_percent"],
    "company_metrics": ["metric", "value", "unit"],
}

# Entity-list sections: full-replacement of table rows. Maps each spec field
# (as it appears in GET data) to the model attribute.
_ENTITY_LIST = {
    "founders": {
        "model": "Founder",
        "map": {
            "name": "name", "role": "designation", "background": "experience",
            "linkedin_url": "linkedin_url", "is_full_time": "is_full_time",
        },
    },
    "key_people": {
        "model": "KeyPerson",
        "map": {
            "name": "name", "role": "designation", "description": "biography",
            "linkedin_url": "linkedin_url",
        },
    },
    "competitors": {
        "model": "Competitor",
        "map": {
            "name": "name", "status": "positioning",
            # CR-12 — what the competitor actually does. The column existed
            # but was neither emitted nor writable.
            "description": "description", "website": "website",
            "funding_usd_mn": "funding_raised_usd",
            "fy_year": "fy_year", "revenue": "revenue",
            "investors": "investors",
        },
        # Req 8 analysis fields. They live in Competitor.extra (a JSON bag)
        # rather than as columns, so they need an explicit list here —
        # without it a full-replacement save silently discarded all eleven.
        "extra": {
            "business_model": "text", "market_positioning": "text",
            "latest_valuation_usd_mn": "number",
            "revenue_growth_pct": "number", "market_share_pct": "number",
            "relative_scale": "text", "key_differentiators": "text_list",
            "strengths": "text_list", "weaknesses": "text_list",
            "ev_revenue_multiple": "number", "ev_ebitda_multiple": "number",
            "recent_activity": ("list", {
                "activity_type": "text", "date": "text",
                "investors_or_acquirers": "text_list",
                "target_company": "text",
                "deal_value_usd_mn": "number",
                "implied_valuation_multiple": "number",
            }),
        },
    },
    "funding_history": {
        "model": "FundingRound",
        "map": {
            "round": "round_name", "date": "announced_date",
            # As reported. `amount_usd_mn` stays mapped for older payloads
            # that still send only the converted figure; `amount` is what a
            # current run sends, alongside its currency and denomination.
            "amount": "amount_value", "currency": "amount_ccy",
            "denomination": "amount_denomination",
            "amount_usd_mn": "amount_value", "status": "amount_basis",
            "pre_money": "pre_money_value", "post_money": "post_money_value",
            "valuation_currency": "valuation_ccy",
            "pre_money_usd_mn": "pre_money_value", "investors": "investors",
            # Req 1 fields — emitted by the serializer, previously dropped
            # on write.
            "post_money_usd_mn": "post_money_value",
            "lead_investors": "lead_investors",
        },
    },
    "recent_news": {
        "model": "NewsItem",
        "map": {
            "title": "headline", "date": "published_date",
            "description": "summary", "source": "publisher", "link": "url",
        },
    },
}


# ---------------------------------------------------------------------------
# Enhancement-set object sections (Req 2 / 5 / 6 / 7 + leadership + multiples).
#
# These were emitted by the serializer and offered an Edit button by the
# client, but had no branch in update_section_from_data — every save returned
# 422 "not editable". They are declared here as nested shapes rather than
# hand-written branches so the read and write schemas stay together.
#
# Shape grammar:
#   "text"           → string
#   "number"         → Decimal-coerced float or None
#   "text_list"      → list of strings
#   ("object", {...})→ nested object with the given field spec
#   ("list", {...})  → list of objects with the given field spec
# ---------------------------------------------------------------------------
_PERSON_SHAPE = {"name": "text", "role": "text", "background": "text",
                 "tenure": "text", "linkedin_url": "text"}

_NESTED_OBJECT_SECTIONS = {
    "investors_and_cap_table": {
        "cap_table_summary": ("object", {
            "total_investors": "number",
            "ownership": ("list", {"stakeholder_category": "text",
                                   "ownership_pct": "number"}),
        }),
        "investors_list": ("list", {
            "investor_name": "text", "investor_type": "text",
            "funding_amount_usd_mn": "number", "valuation_usd_mn": "number",
            "dilution_pct": "number", "date": "text", "round": "text",
        }),
    },
    "company_story_and_usp": {
        "origin_story": "text",
        "brand_evolution": "text",
        "milestones": ("list", {"date": "text", "title": "text",
                                "description": "text"}),
        "usp": "text",
    },
    "industry_and_market_research": {
        "industry_evolution": "text",
        # CR-11 — a market size with no stated source or assumption cannot be
        # checked, so the narrative and the per-figure provenance are part of
        # the section rather than optional extras.
        "market_sizing_narrative": "text",
        "methodology": "text",
        "market_sizing": ("object", {
            "tam": ("object", {"value": "text", "source": "text",
                               "assumptions": "text"}),
            "sam": ("object", {"value": "text", "source": "text",
                               "assumptions": "text"}),
            "som": ("object", {"value": "text", "source": "text",
                               "assumptions": "text"}),
        }),
        "performance_trends": "text_list",
        "regulatory_developments": "text_list",
    },
    "investment_thesis": {
        "opportunity_explanation": "text",
        "leadership_assessment": "text",
        "risks_and_concerns": "text_list",
    },
    "leadership_detail": {
        "cfo": ("object", dict(_PERSON_SHAPE)),
        "founders": ("list", dict(_PERSON_SHAPE)),
        "ceo": ("object", dict(_PERSON_SHAPE)),
        "org_structure_note": "text",
    },
    "derived_multiples": {
        "ev_basis": "text",
        "by_year": ("list", {"financial_year": "number", "ev_usd_mn": "number",
                             "ev_revenue_multiple": "number",
                             "ev_ebitda_multiple": "number"}),
    },
}


# Wire key -> the key this module's shape tables are written against.
#
# The generated profile contract adopted shorter section names than the ones
# these tables were built with. Rather than rename every table key (and break
# any client still PATCHing the old names mid-migration), both spellings are
# accepted and canonicalised here. This is a NAME map only — the storage key,
# and therefore where the data actually lands, is resolved separately by
# ``spec_serializer.storage_key_for``.
_CANONICAL_SECTION_KEY = {
    "news": "recent_news",
    "investors_cap_table": "investors_and_cap_table",
    "company_story": "company_story_and_usp",
    "industry_research": "industry_and_market_research",
}


class SectionUpdateError(ValueError):
    """Raised on an invalid PATCH body; surfaced as 422."""


def _as_decimal(v):
    if v in (None, ""):
        return None
    if isinstance(v, (int, float, Decimal)):
        return Decimal(str(v))
    if isinstance(v, str):
        v_str = v.strip()
        if not v_str:
            return None
        parsed_usd = _parse_display_to_usd_mn(v_str)
        if parsed_usd is not None:
            return Decimal(str(parsed_usd))
        try:
            return Decimal(v_str)
        except (InvalidOperation, ValueError):
            pass
        cleaned = re.sub(r"[^\d.-]", "", v_str)
        if cleaned and cleaned not in (".", "-", ".-", "-."):
            try:
                return Decimal(cleaned)
            except (InvalidOperation, ValueError):
                pass
        return None
    try:
        return Decimal(str(v))
    except (InvalidOperation, ValueError):
        return None


def _as_date(v):
    """Coerce a reported date to ``(YYYY-MM-DD or None, precision)``.

    Sources date things as precisely as they know them, and for a news item or
    a funding round that is routinely a month — "August 2024", never
    "14 August 2024". ``DateField`` rejects ``2024-08`` outright, and because a
    single bad row raises out of the whole entity write, ONE month-precision
    date discarded the entire section: the first complete live run researched
    Zerodha across 235 searches, produced a 288,000-character dossier with
    pages of dated news, and stored no news at all.

    ``FundingRound.date_precision`` already specifies the answer — store the
    first of the period and record how much of it was actually known — so this
    implements a decision the model layer had already made and nothing had yet
    carried out.

    An unparseable value returns ``(None, "")`` rather than raising. Losing a
    date is a smaller loss than losing the row, and far smaller than losing
    every row beside it.
    """
    if v in (None, ""):
        return None, ""
    text = str(v).strip()
    for pattern, precision, suffix in (
            (r"^(\d{4})-(\d{2})-(\d{2})$", "day", ""),
            (r"^(\d{4})-(\d{2})$", "month", "-01"),
            (r"^(\d{4})$", "year", "-01-01")):
        if re.match(pattern, text):
            return text + suffix, precision
    return None, ""


def _as_bool(v, *, default=False):
    """Coerce to a boolean, with an explicit answer for "not stated".

    ``default`` exists because a missing boolean and a false one are different
    facts and the right reading is per field: an absent ``is_founder`` means
    "the model did not classify this person", and the documented rule is that
    such a person counts as a founder. Collapsing that to ``bool(None)`` files
    them as a key person and empties the section a reader checks for the team.
    """
    if isinstance(v, bool):
        return v
    if v is None:
        return default
    if isinstance(v, str):
        text = v.strip().lower()
        if text in ("true", "1", "yes", "y", "t"):
            return True
        if text in ("false", "0", "no", "n", "f"):
            return False
        return default if not text else bool(text)
    if isinstance(v, (int, float, Decimal)):
        return bool(v)
    return bool(v)


def _row_has_real_value(row):
    """True if a dict row carries any non-empty, non-null value — used to
    discard the self-describing null placeholder row on write."""
    for v in row.values():
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, bool):
            if v:
                return True
        elif isinstance(v, (int, float)) and v is not None:
            return True
        elif isinstance(v, (list, dict)) and v:
            return True
    return False


def _as_list(v):
    if v in (None, ""):
        return []
    if isinstance(v, list):
        return [str(x).strip() for x in v if str(x).strip()]
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    return []


def _coerce_shaped(section_key, spec, value):
    """Coerce a PATCH value against a shape spec (see _NESTED_OBJECT_SECTIONS).

    Unknown keys are dropped rather than rejected, so a client that echoes
    back an extra field does not fail the whole save.
    """
    if spec == "text":
        return "" if value is None else str(value)
    if spec == "number":
        d = _as_decimal(value)
        return float(d) if d is not None else None
    if spec == "text_list":
        return _as_list(value)

    kind, inner = spec
    if kind == "object":
        src = value if isinstance(value, dict) else {}
        return {k: _coerce_shaped(section_key, s, src.get(k))
                for k, s in inner.items()}
    if kind == "list":
        if value in (None, ""):
            rows = []
        elif isinstance(value, list):
            rows = value
        else:
            raise SectionUpdateError(
                f"{section_key}: expected an array.")
        out = []
        for row in rows:
            if not isinstance(row, dict):
                raise SectionUpdateError(
                    f"{section_key}: list rows must be objects.")
            out.append({k: _coerce_shaped(section_key, s, row.get(k))
                        for k, s in inner.items()})
        # Drop all-blank template rows the GET sends when a section is empty,
        # so saving an untouched form does not read back as real data.
        return [r for r in out if _row_has_real_value(r)]
    raise SectionUpdateError(f"{section_key}: unsupported field shape.")


def _shaped_has_data(structured):
    """True when any field of a shaped object carries a real value."""
    for v in structured.values():
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, (list, tuple)) and v:
            return True
        if isinstance(v, dict) and _shaped_has_data(v):
            return True
        if isinstance(v, bool):
            if v:
                return True
        elif isinstance(v, (int, float)) and v is not None:
            return True
    return False


def _sanitize_generated(wire_key, data):
    """Type one section's generated payload against its declared field spec.

    Resolves the section by the key the CALLER used, before
    ``_CANONICAL_SECTION_KEY`` rewrites it — that map turns wire names into the
    legacy storage names (``news`` → ``recent_news``), which the schema does not
    know, so canonicalising first would silently find no spec and sanitize
    nothing.

    Never raises. An unrecognised section (one an admin added with no generated
    contract, or a legacy name) falls back to structural sanitization: markup
    still stripped, sizes still bounded, types left alone. Refusing to write a
    section because its spec could not be resolved would trade a formatting
    problem for a missing section.
    """
    try:
        from fundos.profile import sanitize, schema

        if wire_key not in schema.sections_by_key():
            return sanitize.sanitize_json(data)
        return schema.coerce_section_data(wire_key, data)
    except Exception:
        _logger.warning(
            "SECTION WRITE: could not type-check generated data for %s; "
            "writing it unsanitized.", wire_key, exc_info=True)
        return data


def _current_structured(profile, internal_key):
    """The section's stored ``structured`` and whether a human authored it."""
    from fundos.profile.models import ProfileSection

    section = ProfileSection.objects.filter(
        profile=profile, section_key=internal_key, is_active=True).first()
    if section is None:
        return {}, False, None
    return (section.structured or {}), bool(section.edited_by_user), section


def _merge_structured_for_ai(profile, internal_key, incoming):
    """Let a generation run fill in a human-edited section without erasing it.

    The rule the record tables have always followed — an AI write replaces only
    its own previous output — had no equivalent for sections stored in
    ``ProfileSection.structured``, so a full pipeline run silently overwrote a
    founder's hand-edited Business Model or Financial Summary. ``regenerate_
    section`` guards this with a confirm prompt; the full run has no such gate
    and simply wrote through.

    Scalars merge, arrays do not. A blank field is a gap the research should
    close; an array a human curated is authoritative *as a whole*, because its
    meaning is in which rows are present and in what order, and merging by
    index would interleave two different people's lists into one that neither
    intended.

    Returns ``(merged, filled_keys)`` so the caller can say what happened.
    """
    existing, edited_by_user, _ = _current_structured(profile, internal_key)
    if not edited_by_user or not isinstance(existing, dict) or not existing:
        return incoming, []
    if not isinstance(incoming, dict):
        return incoming, []

    merged = dict(existing)
    filled = []
    for key, value in incoming.items():
        current = existing.get(key)
        if isinstance(current, list) and current:
            continue                       # human-curated list — untouched
        if not _is_blank(current):
            continue                       # human supplied it — untouched
        if _is_blank(value):
            continue                       # nothing to contribute
        merged[key] = value
        filled.append(key)
    return merged, filled


def _note_human_clearance(section, previous, incoming, by_ai):
    """Record when a human write empties a section the AI had populated.

    The history snapshot in ``update_section`` already makes the old value
    recoverable, so nothing is lost. What was missing is the *signal*: a
    section reading empty is otherwise indistinguishable from one the research
    looked at and found nothing for, and those call for opposite responses —
    the first needs the founder's own numbers, the second needs a better
    source. Written to ``missing_notes``, which the API already surfaces as
    ``missing``.
    """
    if by_ai or section is None:
        return
    if not _shaped_has_data(previous if isinstance(previous, dict) else {}) or _shaped_has_data(incoming if isinstance(incoming, dict)
                                        else {}):
        return
    from django.utils import timezone

    try:
        notes = list(section.missing_notes or [])
        notes.append(
            f"Cleared by a person on {timezone.now():%Y-%m-%d}. Generated "
            f"content was present before this edit and is recoverable from "
            f"the section's version history; the next generation run will not "
            f"refill it unless it is run with force.")
        section.missing_notes = notes[-20:]
        section.save(update_fields=["missing_notes", "updated_at"])
    except Exception as e:
        _logger.warning("could not record a section clearance: %s", e, exc_info=True)


def _ensure_item_ids(payload):
    """Give every object in a list section a durable id.

    A list section is confirmed item by item, and an item can only be
    addressed by something that survives the next write. Position cannot: let
    a research run insert one founder at the top and "the third founder" is a
    different person, so a confirmation given to Khushboo silently transfers
    to Tanmay.

    Assigned here because this is the one funnel both writers pass through —
    a human PATCH and a generation run — so an id is never missing depending
    on which of them wrote last. An item that already carries one keeps it;
    an item the model returns fresh gets a new one, which means a regenerated
    entry arrives unconfirmed. That is the safe direction: re-confirming a
    rewritten founder costs a click, and inheriting a confirmation the text
    no longer matches costs the truth.
    """
    import uuid

    # A list section stores its rows either as a bare list or nested under
    # `items`; both shapes are already tolerated on the read side, so both
    # have to be reached here or half the sections quietly get no ids.
    rows = payload
    if isinstance(payload, dict) and isinstance(payload.get("items"), list):
        rows = payload["items"]
    if not isinstance(rows, list):
        return payload
    for item in rows:
        if isinstance(item, dict) and not item.get("id"):
            item["id"] = str(uuid.uuid4())
    return payload


def _derive_money(inst):
    """Fill the USD companion from the amount as reported. Never raises.

    Only ever writes the DERIVED fields. The reported three are the fact and
    are left exactly as they arrived, so a rate that turns out to be wrong is
    a correctable error rather than a permanent one — which is the whole
    reason the reported figure is stored at all.

    A currency with no rate leaves the companion BLANK rather than passing the
    number through as though it were already USD. That silent passthrough is
    how INR 56.9 crore came to be stored as 6.828 and labelled INR.
    """
    if not hasattr(inst, "amount_usd_mn"):
        return
    try:
        from fundos.core.services import money

        amount = getattr(inst, "amount_value", None)
        if amount is None:
            return
        currency = getattr(inst, "amount_ccy", "") or ""
        denomination = getattr(inst, "amount_denomination", "") or ""
        if not denomination:
            # A payload that sent only `amount_usd_mn` states no scale,
            # because under the old contract the column name WAS the scale:
            # USD millions, always. Reading that blank as whole units turns
            # $3.2M into $0.0M. The same assumption the data migration wrote
            # onto existing rows is made here for anything still sending the
            # old shape.
            denomination = "Mn"
            inst.amount_denomination = denomination
        # An older payload sends only `amount_usd_mn`, which the map writes to
        # `amount_value` with the default USD currency. That is already the
        # companion's own units, so nothing is converted and nothing is lost.
        usd, rate = money.to_usd_mn(amount, currency, denomination)
        if usd is None:
            return
        inst.amount_usd_mn = usd
        if rate is not None:
            inst.fx_rate = rate
            inst.fx_as_of = getattr(inst, "announced_date", None)
    except Exception:                       # pragma: no cover - never fatal
        _logger.debug("could not derive the USD companion",
                      exc_info=True)


def update_section_from_data(profile, section_key, data, *, user=None,
                             by_ai=False, ai_model_version="",
                             change_summary=""):
    """Apply a standardized ``{data: ...}`` write to any section.

    Serves two callers with the same code path, which is the point: a human
    PATCHing a section and the generation pipeline fanning out a synthesized
    profile must land identically, or the two write paths drift and only one of
    them stays correct.

    ``by_ai`` distinguishes them where it matters — the section's provenance
    and version history record whether a human or a generation run produced the
    current value, which is what lets regeneration refuse to silently discard
    someone's edit.

    Returns the updated ProfileSection (or None for pure entity-list sections
    that have no narrative row). Persists the change, then recomputes
    needs_input + completeness so the response is never a false positive.
    """
    from fundos.platformcfg.services import profile_sections
    from fundos.profile.models import ProfileSection
    from fundos.profile.services import (
        recompute_completeness, recompute_structured_sections, update_section,
    )

    # SANITIZE EVERY AI WRITE, not just the pipeline's.
    #
    # The full run is typed by `schema.normalize_profile` before it gets here,
    # but that is not the only way generated content reaches storage:
    # `POST /profile/sections/{key}/regenerate` and the field-level regenerate
    # call the per-section roles directly and never touch the normalizer. So a
    # regenerated section could still write markdown into a field bound for a
    # PDF, a five-term string into a scalar taxonomy field, or `""` into a
    # numeric column — the exact defects the boundary exists to stop, reachable
    # by a different door.
    #
    # Doing it here instead covers both paths by construction, because this is
    # the one function every AI write goes through. Running twice on the
    # pipeline path costs nothing: the coercions are idempotent, and text that
    # has already been stripped strips to itself.
    if by_ai:
        data = _sanitize_generated(section_key, data)

    section_key = _CANONICAL_SECTION_KEY.get(section_key, section_key)
    label = change_summary or ("AI generation" if by_ai else "Edited")

    # Resolve internal storage key (company_profile is stored as
    # company_overview internally).
    # Req 9: every field of every section must be writable, including the
    # sections renamed on the wire in Req 2/5/6. storage_key_for resolves the
    # rename so a PATCH updates the existing section instead of creating a
    # duplicate under the new name.
    from fundos.profile.spec_serializer import storage_key_for
    internal_key = storage_key_for(section_key)

    valid_keys = {c.section_key for c in profile_sections()}
    if internal_key not in valid_keys:
        raise SectionUpdateError(f"Unknown section '{section_key}'.")

    section = None
    previous_structured, _, _ = _current_structured(profile, internal_key)

    def _store(structured, *, has):
        """Persist one structured section under the write-provenance rules.

        Every structured branch below goes through here rather than calling
        ``update_section`` directly, so the AI-must-not-erase-a-human rule and
        the record-a-clearance signal apply uniformly. They previously applied
        to the record tables only, which is how a full generation run came to
        overwrite hand-edited section JSON without asking.
        """
        payload, filled = ((structured, []) if not by_ai else
                           _merge_structured_for_ai(profile, internal_key,
                                                    structured))
        payload = _ensure_item_ids(payload)
        populated = has or (isinstance(payload, dict)
                            and _shaped_has_data(payload))
        summary = label
        if filled:
            summary = (f"{label} — filled {', '.join(sorted(filled))}; "
                       f"existing edits kept")
        row = update_section(
            profile, internal_key, structured=payload, user=user, by_ai=by_ai,
            ai_model_version=ai_model_version, needs_input=not populated,
            change_summary=summary)
        _note_human_clearance(row, previous_structured, payload, by_ai)
        return row

    if section_key == "ai_company_summary":
        summary = ""
        if isinstance(data, dict):
            summary = str(data.get("summary", "") or "")
        section = update_section(
            profile, internal_key, content=summary, user=user, by_ai=by_ai,
            ai_model_version=ai_model_version,
            needs_input=not summary, change_summary=label)

    elif section_key == "financial_summary":
        if not isinstance(data, dict):
            raise SectionUpdateError("financial_summary expects an object.")
        
        # Handle BOTH formats:
        # 1. Spec-native format (expected): {financials: [...], observations: [...]}
        # 2. Legacy format (from older code): {kind: "fiscal_years", items: [...]}
        financials = []
        observations = []
        
        # Try spec-native format first
        if "financials" in data:
            financials = [r for r in (data.get("financials") or [])
                         if isinstance(r, dict) and _row_has_real_value(r)]
            observations = [o for o in (data.get("observations") or []) if o]
        
        # Fallback to legacy fiscal_years format
        elif data.get("kind") == "fiscal_years":
            items = data.get("items") or []
            for item in items:
                if isinstance(item, dict) and _row_has_real_value(item):
                    mapped = {
                        "financial_year": (item.get("fiscalYear") or 
                                         item.get("financial_year") or 
                                         item.get("year")),
                        "is_estimate": bool(item.get("isEstimate") or 
                                           item.get("is_estimate") or False),
                        "revenue_m": (item.get("revenue") or 
                                     item.get("revenue_m")),
                        "ebitda_m": (item.get("ebitda") or 
                                    item.get("ebitda_m")),
                        "pat_m": (item.get("pat") or 
                                 item.get("pat_m")),
                        "yoy_revenue_growth_pct": (item.get("growthPct") or 
                                                 item.get("growth_pct") or
                                                 item.get("yoy_revenue_growth_pct")),
                        "ev_revenue_multiple": item.get("ev_revenue_multiple"),
                        "currency": (item.get("ccy") or 
                                    item.get("currency")),
                    }
                    clean_mapped = {k: v for k, v in mapped.items() if v is not None}
                    if clean_mapped.get("financial_year"):
                        financials.append(clean_mapped)
            observations = [o for o in (data.get("observations") or []) if o]
        
        structured = {"financials": financials, "observations": observations}
        section = _store(structured, has=bool(financials or observations))
    elif section_key in _OBJECT_FIELDS:
        if not isinstance(data, dict):
            raise SectionUpdateError(f"{section_key} expects an object.")
        allowed = _OBJECT_FIELDS[section_key]
        # Start with the EXISTING stored data as the base, then overlay
        # only the fields the caller actually sent. This prevents a partial
        # PATCH (e.g. a confirmed_fields-only update) from blanking fields
        # that the AI generation populated but the caller didn't include.
        structured = dict(previous_structured) if previous_structured and data else {}
        for f in allowed:
            # Only overwrite if the incoming payload explicitly contains
            # this key. A missing key means "don't touch it", not "clear it".
            if f not in data:
                continue
            if f in _OBJECT_LIST_FIELDS:
                values = _as_list(data.get(f))
                if f == "business_model_types":
                    values = [v for v in values if v in _BUSINESS_MODEL_TYPES]
                structured[f] = values
            elif f in _OBJECT_NUMERIC_FIELDS:
                d = _as_decimal(data.get(f))
                structured[f] = float(d) if d is not None else None
            else:
                structured[f] = data.get(f, "") if data.get(f) is not None else ""
        if section_key == "company_profile":
            # Req 2: parse display strings and derive _usd_mn values, or vice-versa.
            for disp_key, usd_key in _DISPLAY_TO_USD_MN.items():
                prev_usd = (previous_structured or {}).get(usd_key)
                curr_usd = structured.get(usd_key)
                if usd_key in data and (disp_key not in data or curr_usd != prev_usd):
                    if curr_usd is not None:
                        structured[disp_key] = f"USD:{curr_usd:g}:M"
                    else:
                        structured[disp_key] = ""
                elif disp_key in data:
                    disp_val = structured.get(disp_key, "")
                    if disp_val:
                        converted = _parse_display_to_usd_mn(disp_val)
                        if converted is not None:
                            structured[usd_key] = converted
        section = _store(structured, has=_object_has_data(structured))
        if section_key == "company_profile":
            _mirror_company_profile(profile, section.structured or structured)

    elif section_key in _STRUCTURED_LIST_FIELDS:
        if not isinstance(data, list):
            raise SectionUpdateError(f"{section_key} expects an array.")
        allowed = _STRUCTURED_LIST_FIELDS[section_key]
        items = []
        for row in data:
            if not isinstance(row, dict):
                raise SectionUpdateError(f"{section_key} rows must be objects.")
            clean = {}
            if row.get("id"):
                clean["id"] = str(row["id"])
            for f in allowed:
                if f in ("share_percent",):
                    # A SHARE NOBODY STATED IS NOT A SHARE OF ZERO. Coerced
                    # to 0, every stream on a profile whose source never
                    # broke the revenue down read as "earns nothing", which
                    # is a finding -- and one the source never made.
                    clean[f] = _num_or_none(row.get(f))
                else:
                    clean[f] = row.get(f, "")
            items.append(clean)
        section = _store({"items": items}, has=bool(items))

    elif section_key == "founders":
        # One wire section, two storage tables — see _replace_people.
        if not isinstance(data, list):
            raise SectionUpdateError("founders expects an array.")
        section = _replace_people(profile, data, user=user,
                                  source="ai_research" if by_ai else "founder")

    elif section_key in _ENTITY_LIST:
        if not isinstance(data, list):
            raise SectionUpdateError(f"{section_key} expects an array.")
        section = _replace_entity_rows(
            profile, section_key, internal_key, data, user=user,
            source="ai_research" if by_ai else "founder")

    elif section_key in _NESTED_OBJECT_SECTIONS:
        # Req 2 / 5 / 6 / 7 sections plus leadership_detail and
        # derived_multiples. These are stored under their own (unrenamed)
        # storage keys, resolved by storage_key_for above.
        if not isinstance(data, dict):
            raise SectionUpdateError(f"{section_key} expects an object.")
        shape = _NESTED_OBJECT_SECTIONS[section_key]
        structured = {k: _coerce_shaped(section_key, spec, data.get(k))
                      for k, spec in shape.items()}
        section = _store(structured, has=_shaped_has_data(structured))

    else:
        raise SectionUpdateError(f"Section '{section_key}' is not editable.")

    recompute_structured_sections(profile)
    recompute_completeness(profile)
    return section


def _num_or_zero(v):
    d = _as_decimal(v)
    return float(d) if d is not None else 0


def _num_or_none(v):
    """A number, or None when there is no number. Zero survives as zero."""
    d = _as_decimal(v)
    return float(d) if d is not None else None


def _object_has_data(structured):
    for v in structured.values():
        if isinstance(v, list) and v:
            return True
        if isinstance(v, str) and v.strip():
            return True
        if isinstance(v, (int, float)) and v:
            return True
    return False


def _mirror_company_profile(profile, structured):
    """Keep the profile's own website/country/currency in sync with the
    company_profile section so the two never disagree."""
    # THE INDUSTRY A COMPANY CLAIMS BECOMES A PICKLIST VALUE.
    #
    # A founder whose industry is not among the 60 has to put something in
    # the box, and blocking them produces the worst outcome available: the
    # nearest value that lets the form submit, which is wrong and looks
    # deliberate. The label is accepted and marked for review instead.
    #
    # No benchmark cohort is created — see fundos.platformcfg.taxonomy.
    # A SAVEPOINT, NOT JUST A TRY/EXCEPT.
    #
    # `try/except` around a database call is NOT enough to make it harmless:
    # in Postgres a failed statement poisons the whole transaction, so
    # swallowing the error leaves every later write in the same block
    # failing with "current transaction is aborted". That is exactly what
    # happened when this shipped ahead of its migration — the taxonomy
    # insert hit a missing column, the exception was caught, and the
    # company_profile section was then lost to a transaction nobody could
    # tell was already dead.
    #
    # `atomic()` opens a savepoint. A failure inside rolls back to it and
    # the outer transaction carries on intact.
    from django.db import transaction

    try:
        with transaction.atomic():
            from fundos.platformcfg import taxonomy

            taxonomy.record(structured.get("macro_sector"),
                            structured.get("sub_sector"),
                            company=getattr(profile.company, "name", ""))
    except Exception as exc:            # pragma: no cover - never fatal
        _logger.warning("TAXONOMY: not recorded for %s: %s", profile.pk, exc)

    changed = []
    website = structured.get("website")
    country = structured.get("country")
    currency = structured.get("currency_id")
    if website is not None and website != profile.website_url:
        profile.website_url = website
        changed.append("website_url")
    if country is not None and country != profile.hq_country:
        profile.hq_country = country
        changed.append("hq_country")
    if currency is not None and currency != profile.home_currency:
        profile.home_currency = currency
        changed.append("home_currency")
    if changed:
        changed.append("updated_at")
        profile.save(update_fields=changed)


def _is_blank(value):
    """True when an attribute holds nothing a reader would call a value.

    Booleans are never blank — every row has one, and treating ``False`` as
    "unset" is the same mistake that once let an empty Founders section report
    itself complete. ``0`` is likewise a real number, not an absence.
    """
    if value is None:
        return True
    if isinstance(value, bool):
        return False
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, dict)):
        return not value
    return False


_AI_FILLED_MARKER = "ai_filled:"


def _ai_filled_fields(row):
    """The attribute names a previous AI pass supplied on a human-owned row.

    Recorded in ``source_ref`` because provenance is per row and this is per
    field. Without it, the first AI value written into a founder's blank
    ``background`` would be indistinguishable from something the founder typed,
    and every later run would decline to refresh it — the enrichment would
    work exactly once and then freeze.
    """
    ref = getattr(row, "source_ref", "") or ""
    if not ref.startswith(_AI_FILLED_MARKER):
        return set()
    return {part for part in ref[len(_AI_FILLED_MARKER):].split(",") if part}


def _norm_name(value):
    """Case- and whitespace-insensitive key for matching a person or company.

    Deliberately not fuzzy. Merging two records is destructive and a
    near-match here would silently overwrite one person's background with
    another's; an exact match after normalisation is the most this may assume.
    """
    return re.sub(r"\s+", " ", str(value or "")).strip().lower()


def _enrich_row(instance, row, field_map, extra_spec, section_key):
    """Fill a human-owned row from an AI row without overwriting the human.

    The rule an AI write follows for whole rows — replace only your own
    previous output — applied one level down, to fields.

    A founder who types a name and a LinkedIn URL into the onboarding form has
    not thereby asserted that their ``background`` is empty; they have simply
    not filled it in. The previous behaviour skipped the entire row, so on a
    live profile the two actual principals carried a name and a URL and nothing
    else while a part-time co-founder — not named at onboarding, so not
    suppressed — carried a full researched paragraph.

    A field is written when it is currently blank, or when a previous AI pass
    supplied it (so research stays refreshable). A field a human has actually
    filled is never touched.

    :returns: True when anything changed.
    """
    ai_owned = _ai_filled_fields(instance)
    written = set()

    for spec_field, attr in field_map.items():
        if spec_field not in row:
            continue
        current = getattr(instance, attr, None)
        if not (_is_blank(current) or attr in ai_owned):
            continue
        try:
            value = _coerce_entity_value(section_key, spec_field, attr,
                                         row.get(spec_field))
        except SectionUpdateError:
            # One malformed field must not cost the whole enrichment. The
            # human's row is already correct without it.
            continue
        if _is_blank(value):
            continue
        setattr(instance, attr, value)
        written.add(attr)
        if spec_field == "date" and hasattr(instance, "date_precision"):
            instance.date_precision = _as_date(row.get(spec_field))[1]

    if extra_spec and hasattr(instance, "extra"):
        extra = dict(getattr(instance, "extra", None) or {})
        for field, shape in extra_spec.items():
            if field not in row:
                continue
            key = f"extra.{field}"
            if not (_is_blank(extra.get(field)) or key in ai_owned):
                continue
            try:
                value = _coerce_shaped(section_key, shape, row.get(field))
            except SectionUpdateError:
                continue
            if _is_blank(value):
                continue
            extra[field] = value
            written.add(key)
        if written:
            instance.extra = extra

    if not written:
        return False

    marker = _AI_FILLED_MARKER + ",".join(sorted(ai_owned | written))
    instance.source_ref = marker[:255]
    instance.save()
    return True


def _isolated_enrich(instance, row, field_map, extra_spec, section_key):
    """Enrich one row in its own savepoint. Never raises.

    Enrichment is an improvement to data that is already correct without it, so
    it may never be the reason a generation run loses a section. The savepoint
    matters as much as the except: a failed statement inside the caller's
    ``atomic`` block leaves the transaction unusable, and the *next* query —
    belonging to some unrelated row — is the one that dies and gets the blame.
    """
    from django.db import transaction

    try:
        with transaction.atomic():
            return _enrich_row(instance, row, field_map, extra_spec,
                               section_key)
    except Exception:
        _logger.warning(
            "SECTION WRITE: could not enrich %s row %s from generated data; "
            "the existing row is unchanged.", section_key,
            getattr(instance, "pk", "?"), exc_info=True)
        return False


#: Fields whose value describes ONE company. Two rows carrying an identical
#: one are not two findings about two companies; the second is a copy of the
#: first, and copying is how a profile came to list two rivals with the same
#: revenue and the same sentence of description.
_IDENTIFYING_TEXT = ("description", "background", "summary", "headline")


def _dedupe_entity_rows(section_key, rows):
    """One row per name, and no row asserting another row's facts.

    Two things go wrong in a generated list, and neither is specific to any
    company or sector:

    1. The same name appears twice. The later row is merged into the first,
       filling what the first left blank, so nothing researched is lost and
       the reader sees one entry per company.

    2. Two DIFFERENT names carry an identical description. A description is
       about one company; the same words under two names means the second was
       copied, and the figures copied with it are then asserted about a
       company nobody looked up. The row stays -- the competitor is real --
       but the copied text and any figure identical to the row it was copied
       from are dropped rather than presented as findings.

    A row is never deleted for being a duplicate of something, only merged
    into the row it duplicates.
    """
    out, by_name, by_text = [], {}, {}
    for row in rows:
        key = _norm_name(row.get("name"))
        target = by_name.get(key) if key else None
        if target is not None:
            for field, value in row.items():
                if target.get(field) in (None, "", [], {}) and value not in (
                        None, "", [], {}):
                    target[field] = value
            continue

        row = dict(row)
        text = next((_norm_name(row.get(f)) for f in _IDENTIFYING_TEXT
                     if _norm_name(row.get(f))), "")
        source_row = by_text.get(text) if text else None
        if source_row is not None:
            _logger.info(
                "SECTION %s: %r repeats the description given for %r — the "
                "copied text and the figures identical to it are dropped "
                "rather than asserted about a second company.",
                section_key, row.get("name"), source_row.get("name"))
            for field in _IDENTIFYING_TEXT:
                if _norm_name(row.get(field)) == text:
                    row.pop(field, None)
            # Dropped by removing the KEY, not by writing a null: the writer
            # skips a field the payload does not carry, and a null would set
            # a column that cannot hold one.
            for field, value in list(row.items()):
                if (field not in ("name", "website")
                        and value not in (None, "", [], {})
                        and source_row.get(field) == value):
                    row.pop(field, None)
        elif text:
            by_text[text] = row

        if key:
            by_name[key] = row
        out.append(row)
    return out


def _replace_entity_rows(profile, section_key, internal_key, rows, *,
                         user=None, source="founder", update_section_row=True,
                         route=None):
    """Full-replacement write for an entity-list section.

    Deletes existing rows and recreates from the payload, so the body is the
    authoritative full array (matching the GET shape). Returns the section row
    (for the needs_input flag) if one exists.

    :param source: Canonical provenance for the created rows. The pipeline
        passes ``ai_research``; a human PATCH leaves the default.
    :param update_section_row: Whether to reconcile the section's needs_input
        flag here. The founders/key-people split writes two tables against one
        section, so it suppresses this on the first pass and reconciles once
        against the combined result.
    :param route: Optional ``{normalised_name: section_key}`` map. Used by the
        founders/key-people split so a person a human filed in one table is
        enriched there rather than duplicated into the other.
    """
    from fundos.profile import models as m
    from fundos.profile.models import ProfileSection

    spec = _ENTITY_LIST[section_key]
    Model = getattr(m, spec["model"])
    field_map = spec["map"]
    label_field = next((attr for wire, attr in field_map.items()
                        if wire == "name"), "name")
    extra_spec = spec.get("extra") or {}

    rows = _dedupe_entity_rows(section_key,
                               [r for r in (rows or [])
                                if isinstance(r, dict)])

    existing = Model.objects.filter(profile=profile)
    if source == "ai_research":
        # AI GENERATION MUST NOT DELETE OR OVERWRITE WHAT A HUMAN ENTERED.
        #
        # A human PATCH sends the authoritative full array and replaces
        # everything, which is right — the client just rendered those rows and
        # the user edited them. A generation run has not seen the founder's
        # own entries and is in no position to remove them; the previous
        # pipeline was explicit that record tables are "seeded only when
        # empty" for exactly this reason, and a full replacement here would
        # quietly delete a founder's hand-entered cap table on every run.
        #
        # So an AI write replaces only its OWN previous output. Where a human
        # has claimed a name, the AI row is MERGED into that row rather than
        # dropped: it fills the fields the human left blank and refreshes the
        # ones a previous AI pass supplied, and never touches a field the human
        # actually filled. Skipping the whole row — the previous behaviour —
        # protected the human's data by discarding everything researched about
        # them, which is why a live profile carried two principals with an
        # empty `background` beside a co-founder with a full one.
        kept = list(existing.exclude(source="ai_research"))
        claimed = {}
        for keeper in kept:
            key = _norm_name(getattr(keeper, label_field, ""))
            # First row wins on a duplicate name: enriching one of them is
            # right and enriching both would copy one person's research onto
            # two records.
            if key and key not in claimed:
                claimed[key] = keeper
        existing.filter(source="ai_research").delete()

        fresh = []
        for row in rows:
            key = _norm_name(row.get("name"))
            if route is not None and key and route.get(key) not in (
                    None, section_key):
                # A human filed this person in the other table. Their record
                # there is enriched by that table's own pass; creating one here
                # too would show the same person twice.
                continue
            target = claimed.get(key) if key else None
            if target is not None:
                _isolated_enrich(target, row, field_map, extra_spec,
                                 section_key)
            else:
                fresh.append(row)
        rows = fresh
        offset = len(kept)
    else:
        # Human write: the payload is authoritative for the whole section.
        existing.delete()
        offset = 0

    for i, row in enumerate(rows, start=offset):
        if not isinstance(row, dict):
            raise SectionUpdateError(f"{section_key} rows must be objects.")
        # `source` must be one of the canonical provenance values
        # (core.models.base.ProvenanceMixin: ai_research | founder |
        # document_extracted | stage0). Writing a non-canonical
        # "founder_entered" meant onboarding's cleanup filter
        # (source="founder") never matched these rows, so re-submitting the
        # onboarding form duplicated every founder.
        row_id = row.get("id")
        inst = None
        if row_id:
            try:
                import uuid as _uuid
                valid_uuid = _uuid.UUID(str(row_id)) if not isinstance(row_id, _uuid.UUID) else row_id
                inst = Model.all_objects.filter(id=valid_uuid).first()
            except (ValueError, TypeError):
                pass

        if inst is not None:
            inst.is_deleted = False
            inst.deleted_at = None
            inst.deleted_by = None
            inst.source = source
            inst.sort_order = i + 1
            if user is not None:
                inst.created_by = user
        else:
            inst_kwargs = {
                "tenant_id": profile.company.tenant_id,
                "profile": profile,
                "source": source,
                "sort_order": i + 1,
                "created_by": user,
            }
            if row_id:
                try:
                    import uuid as _uuid
                    inst_kwargs["id"] = _uuid.UUID(str(row_id)) if not isinstance(row_id, _uuid.UUID) else row_id
                except (ValueError, TypeError):
                    pass
            inst = Model(**inst_kwargs)
        for spec_field, attr in field_map.items():
            if spec_field not in row:
                continue
            value = _coerce_entity_value(section_key, spec_field, attr,
                                         row.get(spec_field))
            setattr(inst, attr, value)
            # A month-precision date is stored on the first of the month, so
            # without this the row would assert a day nobody reported. Only
            # some of these models carry the column; the rest keep the
            # normalised date and lose the distinction.
            if spec_field == "date" and hasattr(inst, "date_precision"):
                inst.date_precision = _as_date(row.get(spec_field))[1]
        if extra_spec:
            extra = {}
            for field, shape in extra_spec.items():
                if field in row:
                    extra[field] = _coerce_shaped(section_key, shape,
                                                  row.get(field))
            if extra:
                inst.extra = extra
        _derive_money(inst)
        inst.save()

    if not update_section_row:
        return None

    section = ProfileSection.objects.filter(
        profile=profile, section_key=internal_key, is_active=True).first()
    if section is not None:
        # Count what the section HOLDS, not what this write contributed. An
        # AI pass whose rows were all skipped because a human had already
        # entered them wrote nothing and yet the section is fully populated;
        # flagging it "needs input" would ask the founder to fill in work they
        # have already done.
        section.needs_input = not Model.objects.filter(
            profile=profile).exists()
        section.save(update_fields=["needs_input", "updated_at"])

    return section


def _replace_people(profile, rows, *, user=None, source="founder"):
    """Write the combined founders/key-people array into its two tables.

    The generated contract has one ``founders`` section carrying everyone, with
    ``is_founder`` distinguishing a founder from another key person. Storage
    keeps two tables — ``Founder`` and ``KeyPerson`` — because each has its own
    columns, its own records CRUD endpoint and, for founders, the C3
    self-confirmation fields that a key person has no equivalent of.

    Merging them on the wire and splitting them here is deliberate: it is what
    lets the section be one list the model can populate coherently (it knows
    who runs a company, not which of our two tables a person belongs in)
    without stranding the ``KeyPerson`` table or breaking
    ``/profile/records/key_people``.

    A row with no explicit ``is_founder`` counts as a founder: for a company
    profile the founders are the population that matters, and mis-filing a CTO
    as a founder is a smaller error than dropping them from the section a
    reader checks for the team.
    """
    from fundos.profile import models as m
    from fundos.profile.models import ProfileSection

    # Which table a human has already filed each named person into.
    #
    # A generation run classifies people by evidence; a human classified them
    # by opinion, and theirs stands. Without this, a person the founder entered
    # as a Founder whom the research reports as a non-founder executive would
    # be enriched in neither table — the founders pass would see a name it does
    # not have, the key-people pass would create a second record, and the
    # profile would show the same person twice with different detail.
    route = {}
    if source == "ai_research":
        for model, table in ((m.Founder, "founders"),
                             (m.KeyPerson, "key_people")):
            for existing in model.objects.filter(profile=profile).exclude(
                    source="ai_research"):
                key = _norm_name(getattr(existing, "name", ""))
                if key:
                    route.setdefault(key, table)

    founders, key_people = [], []
    for row in rows:
        if not isinstance(row, dict):
            raise SectionUpdateError("founders rows must be objects.")
        placed = route.get(_norm_name(row.get("name")))
        if placed == "key_people":
            is_founder = False
        elif placed == "founders":
            is_founder = True
        else:
            is_founder = _as_bool(row.get("is_founder", True), default=True)
        if is_founder:
            founders.append(row)
        else:
            # KeyPerson's `description` is the wire name for the same content
            # a founder carries as `background`.
            person = dict(row)
            person.setdefault("description", person.get("background", ""))
            key_people.append(person)

    _replace_entity_rows(profile, "founders", "founders", founders,
                         user=user, source=source, update_section_row=False,
                         route=route)
    _replace_entity_rows(profile, "key_people", "key_people", key_people,
                         user=user, source=source, update_section_row=False,
                         route=route)

    section = ProfileSection.objects.filter(
        profile=profile, section_key="founders", is_active=True).first()
    if section is not None:
        # Needs input only when NOBODY is on record. Two reasons this counts
        # what the tables HOLD rather than what this write supplied: a company
        # with key people but no identified founder still has a populated
        # section, and an AI pass whose rows were all skipped because a human
        # had already entered them wrote nothing while the section is full.
        section.needs_input = not (
            m.Founder.objects.filter(profile=profile).exists()
            or m.KeyPerson.objects.filter(profile=profile).exists())
        section.save(update_fields=["needs_input", "updated_at"])
    return section


# Entity fields that must hold a URL when populated.
_URL_FIELDS = {"link", "website", "linkedin_url"}


def _as_url(section_key, spec_field, value):
    """Validate and normalise a URL, or raise a helpful 422."""
    if value in (None, ""):
        return ""
    text = str(value).strip()
    if not text:
        return ""
    # Accept a bare domain and normalise it, so a founder typing
    # "example.com/news/story" is helped rather than rejected.
    candidate = text if "://" in text else f"https://{text}"
    parsed = urlparse(candidate)
    if (parsed.scheme not in ("http", "https") or not parsed.netloc
            or "." not in parsed.netloc or " " in text):
        raise SectionUpdateError(
            f"{section_key}: '{spec_field}' must be a valid link "
            f"(for example https://example.com/article), not free text.",
        )
    return candidate


def _coerce_entity_value(section_key, spec_field, attr, value):
    # Booleans
    if spec_field == "is_full_time":
        return _as_bool(value)
    # Investors (and lead investors) are always a list.
    if spec_field in ("investors", "lead_investors"):
        return _as_list(value)
    # Integer year.
    if spec_field == "fy_year":
        if value in (None, ""):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            raise SectionUpdateError("fy_year must be a whole number.")
    # Numeric money / revenue fields.
    if spec_field in ("funding_usd_mn", "amount_usd_mn", "pre_money_usd_mn",
                      "post_money_usd_mn", "revenue",
                      # As-reported money: the figure the source stated,
                      # whatever currency and scale its siblings name.
                      "amount", "pre_money", "post_money"):
        return _as_decimal(value)
    # Dates. Normalised to a full ISO date Django can store, or None; the
    # precision that was actually reported is recorded separately by the
    # caller on the models that have a column for it.
    if spec_field in ("date",):
        return _as_date(value)[0]
    # CR-14 — URL fields must be a real URL, not free text. Recent News in
    # particular was storing whatever was typed, so a "source link" could be
    # an unverifiable sentence. Empty stays allowed (the field is optional);
    # anything present must parse.
    if spec_field in _URL_FIELDS:
        return _as_url(section_key, spec_field, value)
    # Everything else is text.
    return "" if value is None else str(value)
