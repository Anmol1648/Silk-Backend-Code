"""
Spec-compliant Company Profile serializer (Company_Profile_Generation_Gap
§6, §7, §8, §10.2).

The internal storage model is unchanged: narrative sections live in
``ProfileSection.content``, structured forms in ``ProfileSection.structured``,
and the list sections in their own tables (Founder, KeyPerson, Competitor,
FundingRound, NewsItem). This module is the single place that projects that
internal model onto the CLEAN external contract the client expects:

    {
      "companyId": ..., "companyName": ..., "status": ...,
      "completenessPct": ..., "isComplete": ..., ...,
      "sections": {
        "company_profile": {
          "sectionKey": "company_profile",
          "isComplete": bool,
          "lastUpdatedAt": iso8601 | null,
          "data": { ...Section 7 fields... }
        },
        ...13 more...
      }
    }

Only the 14 schema sections from §7 are emitted. The three internal-only
sections (``knowledge_base``, ``document_center``, ``readiness``) are NOT
part of this contract and are omitted here — they remain reachable through
their own section endpoints for the editor. The internal section key
``company_overview`` is surfaced under the spec name ``company_profile``.

No duplicate top-level entity arrays are emitted: founders / key people /
competitors / funding history / recent news live ONLY inside their
section's ``data`` (§10.2).
"""
from decimal import Decimal


# Internal section key -> external (spec §7) section key. Only differs for
# the renamed overview section; every other key is already spec-aligned.
INTERNAL_TO_SPEC_KEY = {
    "company_overview": "company_profile",
}
SPEC_TO_INTERNAL_KEY = {v: k for k, v in INTERNAL_TO_SPEC_KEY.items()}


def storage_key_for(wire_key):
    """Wire section key -> the section key actually stored in the DB.

    Req 2/5/6 renamed three sections on the API surface only. Resolving here
    means PATCH on a new name lands on the existing stored section rather
    than silently creating a second, empty one alongside it.
    """
    key = _STORAGE_ALIAS.get(wire_key, wire_key)
    return SPEC_TO_INTERNAL_KEY.get(key, key)

# The external contract, in schema order.
#
# Resolved from `fundos.profile.schema`, which is the single definition of the
# generated profile: the same section list and field specs that build the
# generation prompt and normalise the response. Reading it here is what stops
# the API contract and the thing the model is asked to produce from drifting
# apart — they were separate constants before, and keeping two hand-maintained
# copies of a twenty-section schema in agreement is not a thing that happens.
#
# A module-level fallback keeps import-time safety: schema.section_keys()
# touches the database, and this module is imported during migrations.
_FALLBACK_SECTION_ORDER = [
    "company_profile", "founders", "products_services", "customers_markets",
    "competitive_advantages", "business_model", "revenue_model",
    "company_metrics", "financial_summary", "funding_history", "competitors",
    "news", "investors_cap_table", "company_story", "industry_research",
    "investment_thesis", "document_center",
]


def spec_section_order():
    """The wire sections to emit, in order."""
    try:
        from fundos.profile import schema
        return schema.section_keys() or list(_FALLBACK_SECTION_ORDER)
    except Exception:
        return list(_FALLBACK_SECTION_ORDER)


# Retained as a module constant because tests and older callers import it.
SPEC_SECTION_ORDER = _FALLBACK_SECTION_ORDER

# Wire section key -> underlying storage section key, where they differ.
# Storage names are left alone so no migration or data move is needed; only
# the API surface adopts the contract's names. Both the current names and the
# previous ones resolve, so a client mid-migration is not broken by a rename.
_STORAGE_ALIAS = {
    "investors_cap_table": "cap_table",
    "company_story": "company_story",
    "industry_research": "market_research",
    "news": "recent_news",
    # Previous wire names.
    "investors_and_cap_table": "cap_table",
    "company_story_and_usp": "company_story",
    "industry_and_market_research": "market_research",
}

# Storage key -> the CURRENT wire name. Written out rather than inverted from
# _STORAGE_ALIAS: that dict is many-to-one (two wire names per storage key,
# current and legacy), so inverting it silently picks whichever happens to be
# last in the literal — and the answer would be the retired name.
_WIRE_FOR_STORAGE = {
    "cap_table": "investors_cap_table",
    "company_story": "company_story",
    "market_research": "industry_research",
    "recent_news": "news",
}

# Sections whose value is a JSON object ({}); the rest are arrays ([]).
_OBJECT_SECTIONS = {
    "company_profile", "business_model", "financial_summary",
    "ai_company_summary",
    "investors_cap_table", "company_story", "industry_research",
    "investors_and_cap_table", "company_story_and_usp",
    "industry_and_market_research", "investment_thesis",
    "leadership_detail", "derived_multiples", "document_center",
}

# Empty-row templates for the LIST sections (§7 field schema). When a list
# section has no stored rows, the API returns a single template row with all
# fields present and empty, so the client always sees the field structure —
# it never has to guess the shape from an empty array. `isComplete` stays
# false for these template-only sections (see _section_is_complete).
_LIST_ROW_TEMPLATES = {
    "founders": {"id": "", "name": "", "role": "", "background": "",
                 "linkedin_url": "", "is_full_time": False,
                 "is_founder": True},
    "key_people": {"name": "", "role": "", "description": "",
                   "linkedin_url": ""},
    "products_services": {"id": "", "name": "", "category": "",
                          "description": ""},
    "customers_markets": {"id": "", "market": "", "customer_type": "",
                          "geography": ""},
    "competitive_advantages": {"id": "", "title": "", "description": ""},
    "revenue_model": {"id": "", "stream": "", "share_percent": None},
    "company_metrics": {"id": "", "metric": "", "value": "", "unit": ""},
    # Req 1: `status` removed; post_money_usd_mn and lead_investors added.
    "funding_history": {"id": "", "date": "", "round": "",
                        "amount": None, "currency": "", "denomination": "",
                        "amount_display": "",
                        "amount_usd_mn": None, "fx_rate": None,
                        "fx_as_of": "",
                        "pre_money_usd_mn": None, "post_money_usd_mn": None,
                        "investors": [], "lead_investors": []},
    # Req 8: existing six fields unchanged; the rest are new. Every key is
    # present even when empty — the frontend maps over keys to render.
    # `fy_year` and `revenue` are numbers, so their empty value is null — not
    # "". The template is what a client renders before anything has filled the
    # section, so a "" here taught every consumer that this column may be a
    # string, and the populated rows then disagreed with each other.
    "competitors": {"id": "", "name": "", "description": "", "website": "",
                    "fy_year": None, "revenue": None,
                    "funding_usd_mn": None, "status": "", "investors": [],
                    "business_model": "", "market_positioning": "",
                    "latest_valuation_usd_mn": None,
                    "revenue_growth_pct": None, "market_share_pct": None,
                    "relative_scale": "", "key_differentiators": [],
                    "strengths": [], "weaknesses": [],
                    "ev_revenue_multiple": None, "ev_ebitda_multiple": None,
                    "recent_activity": []},
    "recent_news": {"id": "", "title": "", "date": "", "description": "",
                    "source": "",
                    "link": ""},
}
# The contract renamed recent_news -> news; the template is the same shape.
_LIST_ROW_TEMPLATES["news"] = _LIST_ROW_TEMPLATES["recent_news"]


def _item_id(item):
    """The id a stored list item carries, or "" if it has none yet.

    An item is confirmed BY this. Anything derived from position would move
    the moment a research run inserts a row above it, quietly transferring a
    confirmation to a different product; anything derived from the text would
    move the moment the text is corrected. So the id is stored with the item
    and read back — never computed here.

    Items written before ids existed return "" and are simply not
    individually confirmable until `backfill_profile_item_ids` runs.
    """
    return str((item or {}).get("id") or "") if isinstance(item, dict) else ""


def _with_template(spec_key, rows):
    """Return the section's rows, or a single empty template row when there
    are none, so field structure is always visible in the response.
    """
    if rows:
        return rows
    template = _LIST_ROW_TEMPLATES.get(spec_key)
    return [dict(template)] if template is not None else []


def _f(value):
    """Decimal/None-safe float."""
    if value is None:
        return None
    if isinstance(value, Decimal):
        return float(value)
    return value


def _num(value):
    """A number or ``None`` — never ``""``, never a numeric string.

    The GET contract declares these fields ``number|null``, and a live profile
    shipped ``revenue: ""`` next to ``revenue: 28.5`` next to
    ``funding_usd_mn: null`` inside one competitors array: three spellings of
    "absent" in two columns of the same six rows. A client doing arithmetic
    gets ``NaN`` on the ones that used the empty string, and the ones that used
    a numeric string work only because JavaScript coerces them — until a
    thousands separator arrives and it silently does not.

    Reading from storage rather than from the model, so this is the last place
    the contract can be made true regardless of what any historic write left
    behind.
    """
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, Decimal):
        return float(value)
    if isinstance(value, (int, float)):
        return None if value != value else float(value)     # drop NaN
    from fundos.profile.sanitize import as_number
    return as_number(value)


def _iso(dt):
    return dt.isoformat() if dt else None


# ---------------------------------------------------------------------------
# Per-section data builders. Each returns the exact §7 shape for that section.
# `sec` is the ProfileSection row (may be None); `ctx` carries prefetched
# related rows and the profile so builders don't each hit the DB.
# ---------------------------------------------------------------------------
def _data_company_profile(sec, ctx):
    """company_profile.data — now also carries the Req 1 funding summary."""
    s = (sec.structured if sec else None) or {}
    profile = ctx["profile"]
    # Prefer explicit structured values the AI/editor stored; fall back to the
    # authoritative onboarding inputs on the profile itself.
    data = {
        "description_of_business": s.get("description_of_business",
                                         (sec.content if sec else "") or ""),
        "website": s.get("website", profile.website_url or ""),
        "country": s.get("country", profile.hq_country or ""),
        "macro_sector": s.get("macro_sector", ""),
        "sub_sector": s.get("sub_sector", ""),
        # Req 1 note: funding_status_name is unchanged and continues to serve
        # as the "last funding round name".
        "funding_status_name": s.get("funding_status_name", ""),
        "revenue_size_name": s.get("revenue_size_name", ""),
        "currency_id": s.get("currency_id", profile.home_currency or ""),
    }
    # Req 1: summary figures for the top of the Funding & Valuation panel.
    #
    # THE COMPANY'S OWN FUNDING TABLE IS THE PRIORITY SOURCE for the total.
    # A stated total reaches `structured` from research — a press round-up,
    # an aggregator, a figure in a deck — and it disagreed with the rounds
    # the same profile lists. Preferring it meant the panel showed one
    # number while the table underneath it added to another, with nothing
    # saying so.
    #
    # The stated figure is not discarded: it is carried beside the derived
    # one and the gap is named, because a table missing a round and a web
    # figure counting debt look identical from here and only a reader can
    # tell them apart.
    #
    # With no rounds recorded there is nothing to prefer, so a stated total
    # stands alone — which is also how an editor-entered total survives on a
    # profile whose history has not been filled in.
    derived = _funding_summary(ctx)
    for k, v in derived.items():
        stated = s.get(k)
        if k in _FUNDING_TABLE_WINS and v not in (None, ""):
            data[k] = v
            continue
        data[k] = stated if stated not in (None, "") else v

    gap = _funding_total_gap(s.get("total_funding_raised_usd_mn"),
                             derived.get("total_funding_raised_usd_mn"))
    data["total_funding_raised_reported_usd_mn"] = gap[0]
    data["total_funding_raised_gap"] = gap[1]

    # Req 2: display-formatted funding fields (CURRENCY:AMOUNT:SCALE).
    # An explicit founder-entered string always wins: it carries the currency
    # and scale they actually chose, which the USD figure has already lost.
    #
    # Next comes the figure as the rounds themselves report it — ₹52.25 Cr
    # when every round was raised in crore. Only when neither exists is the
    # display derived from the USD figure, rather than left blank: the
    # conversion used to run one way only — a display string was parsed INTO
    # `_usd_mn` — so a figure derived from funding history (6.27) arrived
    # with an empty display beside it, and the screen showed nothing next to
    # a number the API was holding all along.
    #
    # That fallback renders as USD millions because that is exactly what the
    # stored value is: no FX is applied and nothing is inferred about the
    # founder's preferred currency. `_parse_display_to_usd_mn` reads it back
    # to the same number, so the pair cannot drift.
    for display_key, usd_key in (
            ("total_funding_raised_display", "total_funding_raised_usd_mn"),
            ("latest_pre_money_display", "latest_pre_money_usd_mn"),
            ("latest_post_money_display", "latest_post_money_usd_mn")):
        explicit = s.get(display_key) or ""
        if explicit:
            data[display_key] = explicit
            continue
        data[display_key] = (derived.get(display_key)
                             or _usd_mn_as_display(data.get(usd_key)))
    return data


#: Keys whose value the funding rows themselves settle. A figure derived from
#: the company's own table is preferred over one stated in `structured`,
#: which is where research puts a number it read somewhere else.
_FUNDING_TABLE_WINS = frozenset({
    "total_funding_raised_usd_mn",
    "total_funding_raised_amount",
    "total_funding_raised_currency",
    "total_funding_raised_denomination",
    "total_funding_raised_basis",
})

#: How far apart two totals may be before the difference is worth showing.
#: Rounding, an undisclosed round size and a stale rate all move a total by a
#: little; 5% is past all three.
_FUNDING_GAP_TOLERANCE = 0.05


def _funding_total_gap(stated, derived):
    """The stated total and a sentence about how it differs, or (None, "").

    Says nothing when the two agree, when only one exists, or when the
    stated value is not a number — a gap line on every profile would be
    noise, and this one is meant to be read.
    """
    reported = _num(stated)
    if reported is None or derived in (None, ""):
        return (reported, "")
    try:
        table = float(derived)
    except (TypeError, ValueError):            # pragma: no cover - defensive
        return (reported, "")
    if table <= 0:
        return (reported, "")
    if abs(reported - table) <= _FUNDING_GAP_TOLERANCE * table:
        return (reported, "")
    direction = "more" if reported > table else "less"
    return (reported,
            f"Research states US${reported:,.4g} Mn raised — "
            f"US${abs(reported - table):,.4g} Mn {direction} than the "
            f"rounds recorded here add up to (US${table:,.4g} Mn). "
            f"Both are shown; neither has been "
            f"changed to match the other.")


#: The display contract is CURRENCY:AMOUNT:SCALE, and its scale tokens are
#: not the storage ones: millions is "M" here and "Mn" on the row.
_DISPLAY_SCALES = {"": "", "K": "K", "L": "L", "Mn": "M", "Cr": "Cr",
                   "Bn": "B"}


def _reported_display(amount, currency, denomination):
    """A figure as its source wrote it, in the display contract's shape.

    "" when there is no figure — an absent amount must stay absent rather
    than render as zero. `_parse_display_to_usd_mn` reads every string this
    returns, so the display and the USD figure beside it cannot drift.
    """
    if amount in (None, ""):
        return ""
    try:
        value = float(amount)
    except (TypeError, ValueError):
        return ""
    scale = _DISPLAY_SCALES.get(denomination or "")
    if scale is None:
        return ""
    text = f"{value:.4f}".rstrip("0").rstrip(".") or "0"
    return f"{(currency or 'USD').upper()}:{text}:{scale}"


def _usd_mn_as_display(value):
    """A USD-millions figure as a CURRENCY:AMOUNT:SCALE display string.

    Returns "" for a missing value: an absent figure must stay absent rather
    than be shown as zero.
    """
    if value in (None, ""):
        return ""
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return ""
    # Trailing zeros trimmed so 6.27 reads as "6.27" and 100.0 as "100".
    text = f"{amount:.4f}".rstrip("0").rstrip(".") or "0"
    return f"USD:{text}:M"


def _data_founders(sec, ctx):
    """Founders AND other key people, in one list, flagged by `is_founder`.

    Storage keeps two tables — Founder carries the C3 self-confirmation fields
    a key person has no equivalent of, and each has its own records endpoint —
    but the generated contract presents one section, because "who runs this
    company" is one question and a model asked to sort people into our two
    tables will get it wrong. Founders come first: they are what a reader
    checks for.
    """
    # `id` is the ROW's own primary key, not a number invented for the
    # payload. It is what makes a Confirm stick to the person it was given
    # to: position cannot, because one founder inserted at the top turns
    # "the third founder" into somebody else, and a name cannot, because
    # names get corrected. Each person is one part with one Confirm, and this
    # is the part's address.
    rows = [{
        "id": str(f.id),
        "name": f.name or "",
        "role": f.designation or "",
        # §7.2 — a 2–3 line executive summary. `experience` is our closest
        # short field; fall back to biography if experience is empty.
        "background": (f.experience or f.biography or ""),
        "linkedin_url": f.linkedin_url or "",
        "is_full_time": bool(f.is_full_time),
        "is_founder": True,
    } for f in ctx["founders"]]
    rows += [{
        "id": str(p.id),
        "name": p.name or "",
        "role": p.designation or "",
        "background": p.biography or "",
        "linkedin_url": p.linkedin_url or "",
        "is_full_time": True,
        "is_founder": False,
    } for p in ctx["key_people"]]
    return _with_template("founders", rows)


def _data_document_center(sec, ctx):
    """Section 8.17, from the Document Center — never from the model.

    The pipeline knows exactly what was uploaded and exactly how each file was
    read. Asking a model to describe that would invite it to invent document
    metadata that looks plausible and is not real.
    """
    extraction = ctx.get("extraction") or {}
    rows = []
    for d in ctx.get("documents") or []:
        detail = extraction.get(d.material_asset_id) or {}
        rows.append({
            "id": str(d.id),
            "filename": d.filename or "",
            "category": d.get_category_display(),
            # "Uploaded" is the honest default: no extraction record means the
            # file has not been read yet, which is a different statement from
            # "it was read and yielded nothing".
            "status": {"extracted": "Processed",
                       "failed": "Failed"}.get(detail.get("status"),
                                               "Uploaded"),
            "handler": detail.get("handler") or "",
            "ocr_status": detail.get("ocr_status") or "",
            "ocr_reason": detail.get("ocr_reason") or "",
        })
    return {"documents": rows}


def _data_key_people(sec, ctx):
    rows = [{
        "name": p.name or "",
        "role": p.designation or "",
        "description": p.biography or "",
        "linkedin_url": p.linkedin_url or "",
    } for p in ctx["key_people"]]
    return _with_template("key_people", rows)


def _data_products_services(sec, ctx):
    items = _structured_list(sec, "items")
    if items:
        return [{
            "id": _item_id(it),
            "name": it.get("name", ""),
            "category": it.get("category", ""),
            "description": it.get("description", ""),
        } for it in items]
    # Content-fallback (Gap2): older generations left the AI finding in
    # `content` only. Surface it so nothing is lost until a regenerate fills
    # the structured shape properly.
    note = _content_note(sec)
    if note:
        return [{"id": "", "name": "", "category": "",
                 "description": note}]
    return _with_template("products_services", [])


def _data_customers_markets(sec, ctx):
    items = _structured_list(sec, "items")
    if items:
        return [{
            "id": _item_id(it),
            "market": it.get("market", ""),
            "customer_type": it.get("customer_type", ""),
            "geography": it.get("geography", ""),
        } for it in items]
    note = _content_note(sec)
    if note:
        return [{"id": "", "market": "", "customer_type": "",
                 "geography": note}]
    return _with_template("customers_markets", [])


def _data_competitive_advantages(sec, ctx):
    items = _structured_list(sec, "items")
    if items:
        out = []
        for it in items:
            # §7.6 — never plain strings; always title + description objects.
            if isinstance(it, str):
                out.append({"id": "", "title": it, "description": ""})
            else:
                out.append({"id": _item_id(it),
                            "title": it.get("title", ""),
                            "description": it.get("description", "")})
        return out
    note = _content_note(sec)
    if note:
        return [{"id": "", "title": "", "description": note}]
    return _with_template("competitive_advantages", [])


def _data_business_model(sec, ctx):
    s = (sec.structured if sec else None) or {}
    channels = s.get("distribution_channels", [])
    if isinstance(channels, str):
        channels = [c.strip() for c in channels.split(",") if c.strip()]
    # Req 4: explicit classification, rather than the frontend inferring the
    # model type from prose. Read from either key; generation emits `models`.
    types = s.get("business_model_types") or s.get("models") or []
    if isinstance(types, str):
        types = [t.strip() for t in types.split(",") if t.strip()]
    allowed = {"B2B", "B2C", "B2B2C", "D2C", "SaaS", "Marketplace", "Other"}
    types = [t for t in types if t in allowed]

    data = {
        "business_model_types": types,
        "customer_type": s.get("customer_type", ""),
        "value_proposition": s.get("value_proposition", ""),
        "delivery_model": s.get("delivery_model", ""),
        "pricing_model": s.get("pricing_model", ""),
        "sales_model": s.get("sales_model", ""),
        "distribution_channels": channels,
    }
    # Content-fallback (Gap2): if the structured object is entirely empty but
    # the AI wrote a prose finding, surface it in value_proposition so the
    # generated content isn't lost.
    if not any(v for k, v in data.items() if k != "business_model_types"):
        note = _content_note(sec)
        if note:
            data["value_proposition"] = note
    return data


def _data_revenue_model(sec, ctx):
    items = _structured_list(sec, "items")
    rows = [{
        "id": _item_id(it),
        # Storage uses {label, pct}; the spec output uses {stream, share_percent}.
        "stream": it.get("stream", it.get("label", "")),
        # `or 0` turned every unstated share into a stated zero, and a
        # stream reported as earning 0% of revenue is a claim about the
        # business. Null means the split was never given; 0 means it was.
        "share_percent": _num(_first_present(it, "share_percent", "pct",
                                             default=None)),
    } for it in items]
    return _with_template("revenue_model", rows)


def _data_company_metrics(sec, ctx):
    items = _structured_list(sec, "items")
    rows = [{
        "id": _item_id(it),
        # Storage uses {label, value, unit}; spec output uses {metric,value,unit}.
        "metric": it.get("metric", it.get("label", "")),
        "value": _first_present(it, "value", default=""),
        "unit": it.get("unit", ""),
    } for it in items]
    return _with_template("company_metrics", rows)


def _data_financial_summary(sec, ctx):
    s = (sec.structured if sec else None) or {}

    # Storage may hold either the spec-native shape ({financials, observations})
    # or the generic numeric-form shape ({kind: "fiscal_years", items: [...]})
    # that AI generation produces. Read whichever is present so the section
    # is never silently empty (backend-issues combined report #1).
    financials_raw = s.get("financials")
    if financials_raw is None:
        financials_raw = s.get("items") or []
    observations_raw = s.get("observations") or []

    financials = []
    for r in financials_raw:
        if not isinstance(r, dict):
            continue
        # Req 3: year -> financial_year, growth_pct -> yoy_revenue_growth_pct
        # (renames); is_estimate and ev_revenue_multiple are new. Both old and
        # new keys are READ from storage so historic rows keep working.
        fy = _first_present(r, "financial_year", "year", "fiscalYear",
                            default=None)
        financials.append({
            "financial_year": fy,
            "is_estimate": bool(_first_present(r, "is_estimate",
                                               "isEstimate", default=False)),
            "revenue_m": _num(_first_present(r, "revenue_m", "revenue",
                                             default=None)),
            "ebitda_m": _num(_first_present(r, "ebitda_m", "ebitda",
                                            default=None)),
            # PAT and the currency are declared on this section in
            # `schema.SHIPPED_SECTIONS` and were asked for on every run, and
            # neither was ever emitted — so a client had no way to render a
            # figure the model had supplied, and no way to tell what
            # denomination the numbers beside it were in. A money column with
            # no currency is the defect that let INR crore sit in a field named
            # `_m` on a live profile with nothing contradicting it.
            "pat_m": _num(_first_present(r, "pat_m", "pat", default=None)),
            "yoy_revenue_growth_pct": _num(_first_present(
                r, "yoy_revenue_growth_pct", "growth_pct", "growthPct",
                default=None)),
            "ev_revenue_multiple": _num(_first_present(
                r, "ev_revenue_multiple", "evRevenueMultiple", default=None)),
            "currency": (str(_first_present(r, "currency", "ccy", default="")
                             or "").strip().upper()
                         or (ctx["profile"].home_currency or "").upper()),
        })

    observations = []
    for o in observations_raw:
        if isinstance(o, str):
            observations.append(o)
        elif isinstance(o, dict):
            # Tolerate an object form; surface its text.
            observations.append(o.get("title") or o.get("description") or "")

    # Self-describing empty state: when there is no financial data, still send
    # one placeholder row with every field key present and null, so the client
    # can render the correct columns and distinguish "no data yet" (all null)
    # from a missing section — mirroring how business_model always sends its
    # full key set (issue-financial-summary-and-sort-order.md, Issue 1).
    if not financials:
        financials = [{"financial_year": None, "is_estimate": False,
                       "revenue_m": None, "ebitda_m": None,
                       "yoy_revenue_growth_pct": None,
                       "ev_revenue_multiple": None}]

    return {"financials": financials, "observations": observations}


def _first_present(d, *keys, default=None):
    for k in keys:
        if k in d and d[k] not in (None, ""):
            return d[k]
    return default


def _reported_date(value, precision):
    """Render a stored date back at the precision it was reported with.

    A round announced in "August 2010" is stored on the first of the month
    (`FundingRound.date_precision` sets out why), so emitting the raw column
    would tell a client the round closed on the 1st — a day no source stated.
    Emitting "2010-08" instead is both honest and exactly what came in, so the
    read-edit-PATCH round trip this module promises holds: the writer parses
    "2010-08" back to the same date and the same precision.

    The precision is carried by the string's own shape rather than by a second
    field, so the §7 row keeps the six keys it has always had and a client
    that wants to render "August 2010" needs no extra logic. A client that
    parses `date` strictly must accept `YYYY` and `YYYY-MM` alongside a full
    ISO date.
    """
    if not value:
        return ""
    if precision == "month":
        return value.strftime("%Y-%m")
    if precision == "year":
        return value.strftime("%Y")
    return value.isoformat()


def _data_funding_history(sec, ctx):
    """Req 1. `status` removed; post_money_usd_mn and lead_investors added.

    lead_investors is a SUBSET of investors, so it is never populated with a
    name that is not also in `investors` — the frontend renders leads as a
    highlight over the full list, and an orphan lead would render as a ghost.
    """
    rows = []
    for r in ctx["funding_rounds"]:
        investors = r.investors or []
        leads = [i for i in (getattr(r, "lead_investors", None) or [])
                 if i in investors] or (
                     getattr(r, "lead_investors", None) or [])
        rows.append({
            "id": str(r.id),
            "date": _reported_date(r.announced_date, r.date_precision),
            "round": r.round_name or "",
            # As reported, then the derived companion. `amount_usd_mn` keeps
            # its name and its place for every existing consumer, but it is
            # now computed from the three fields above rather than being the
            # only thing stored — so ₹20 crore reads as ₹20 crore and still
            # sorts against a US$ round.
            "amount": _f(r.amount_value),
            "currency": getattr(r, "amount_ccy", "") or "",
            "denomination": getattr(r, "amount_denomination", "") or "",
            "amount_display": _money_display(r),
            "amount_usd_mn": _f(getattr(r, "amount_usd_mn", None)
                                if getattr(r, "amount_usd_mn", None)
                                is not None else r.amount_value),
            "fx_rate": _f(getattr(r, "fx_rate", None)),
            "fx_as_of": (r.fx_as_of.isoformat()
                         if getattr(r, "fx_as_of", None) else ""),
            "pre_money_usd_mn": _f(r.pre_money_value),
            "post_money_usd_mn": _f(getattr(r, "post_money_value", None)),
            "investors": investors,
            "lead_investors": leads,
        })
    return _with_template("funding_history", rows)


def _money_display(row):
    """"₹20 Cr" — the amount as the source wrote it, ready to render."""
    try:
        from fundos.core.services import money
        return money.display(getattr(row, "amount_value", None),
                             getattr(row, "amount_ccy", "") or "",
                             getattr(row, "amount_denomination", "") or "")
    except Exception:                       # pragma: no cover - never fatal
        return ""


def _funding_summary(ctx):
    """Req 1. Summary figures for the top of the Funding & Valuation panel.

    THE ROUNDS ARE THE SOURCE. The total is a sum of the USD companions the
    rounds themselves carry, not of `amount_value` — which, since a round
    keeps the currency and scale its source used, may be 20 (₹20 crore)
    sitting beside 5 (US$5 Mn). Adding those gives 25 of nothing.

    A round whose currency has no rate is COUNTED AS MISSING rather than
    added in as if it were dollars, and `total_funding_raised_basis` says how
    many rounds the figure covers, so a total that is short says so instead
    of looking complete.

    Every key is always present; a figure that cannot be derived is null
    rather than absent or zero — zero would render as a real number.
    """
    from fundos.core.services import money

    rounds = list(ctx["funding_rounds"])
    with_amount = [r for r in rounds if _f(r.amount_value) is not None
                   or _f(getattr(r, "amount_usd_mn", None)) is not None]

    converted, currencies, denominations = [], set(), set()
    for r in with_amount:
        usd, _rate = money.reported_usd_mn(
            _f(r.amount_value),
            getattr(r, "amount_ccy", "") or "",
            getattr(r, "amount_denomination", "") or "",
            derived=_f(getattr(r, "amount_usd_mn", None)))
        if usd is not None:
            converted.append(usd)
        currencies.add((getattr(r, "amount_ccy", "") or "USD").upper())
        denominations.add(getattr(r, "amount_denomination", "") or "Mn")

    total = round(sum(converted), 4) if converted else None

    # When every round was reported in one currency at one scale, the sum is
    # sayable in the company's own terms too — ₹52 Cr rather than only its
    # dollar equivalent.
    reported_total = reported_ccy = reported_denom = None
    if with_amount and len(currencies) == 1 and len(denominations) == 1:
        amounts = [_f(r.amount_value) for r in with_amount]
        if all(a is not None for a in amounts):
            reported_total = round(sum(amounts), 4)
            reported_ccy = next(iter(currencies))
            reported_denom = next(iter(denominations))

    dated = [r for r in rounds if r.announced_date]
    latest = max(dated, key=lambda r: r.announced_date) if dated else None

    out = {
        "total_funding_raised_usd_mn": total,
        "total_funding_raised_amount": reported_total,
        "total_funding_raised_currency": reported_ccy or "",
        "total_funding_raised_denomination": reported_denom or "",
        "total_funding_raised_display": _reported_display(
            reported_total, reported_ccy, reported_denom),
        "total_funding_raised_basis": _total_basis(len(with_amount),
                                                   len(converted)),
        "last_funding_round_date": (latest.announced_date.isoformat()
                                    if latest else None),
    }

    # Pre- and post-money were stored in `valuation_ccy` under column names
    # that asserted USD millions. The figure is left exactly as reported and
    # the USD companion derived beside it, the same way an amount is.
    for key, attr in (("latest_pre_money", "pre_money_value"),
                      ("latest_post_money", "post_money_value")):
        value = _f(getattr(latest, attr, None)) if latest else None
        ccy = (getattr(latest, "valuation_ccy", "") or "USD") if latest else ""
        usd, _rate = money.reported_usd_mn(value, ccy, "Mn")
        out[f"{key}_usd_mn"] = usd
        out[f"{key}_amount"] = value
        out[f"{key}_currency"] = ccy if value is not None else ""
        out[f"{key}_denomination"] = "Mn" if value is not None else ""
        out[f"{key}_display"] = _reported_display(value, ccy, "Mn")
    return out


def _total_basis(counted, converted):
    """One sentence naming what the total covers, or "" when it covers all.

    Generic by construction: it counts rounds, and names no company, currency
    or source.
    """
    if not counted:
        return ""
    if converted == counted:
        return f"company funding history: {counted} round(s)"
    return (f"company funding history: {converted} of {counted} round(s) — "
            f"{counted - converted} could not be expressed in USD and are "
            f"NOT included in this total")


def _data_competitors(sec, ctx):
    """Req 8. The original six fields are unchanged; the rest are new.

    New fields live in Competitor.extra (a JSON column) rather than getting
    a dedicated DB column each — they are display-only analysis fields that
    the frontend and the AI both treat as a bag, and 11 sparse columns would
    buy nothing. Every key is emitted even when unset.
    """
    out = []
    for c in ctx["competitors"]:
        x = (getattr(c, "extra", None) or {})
        out.append({
            "id": str(c.id),
            # --- existing, unchanged ---
            "name": c.name or "",
            # CR-12: without a plain description of what the competitor
            # actually does, neither the reader nor the AI can judge whether
            # a listed company is a genuine comparable. The column already
            # existed on the model; it was simply never emitted.
            "description": c.description or "",
            "website": c.website or "",
            "fy_year": _num(c.fy_year),
            "revenue": _num(c.revenue),
            "funding_usd_mn": _num(c.funding_raised_usd),
            "status": c.positioning or "",
            "investors": c.investors or [],
            # --- Req 8, new ---
            "business_model": x.get("business_model", "") or "",
            "market_positioning": x.get("market_positioning", "") or "",
            "latest_valuation_usd_mn": _num(x.get("latest_valuation_usd_mn")),
            "revenue_growth_pct": _num(x.get("revenue_growth_pct")),
            "market_share_pct": _num(x.get("market_share_pct")),
            "relative_scale": x.get("relative_scale", "") or "",
            "key_differentiators": x.get("key_differentiators") or [],
            "strengths": x.get("strengths") or [],
            "weaknesses": x.get("weaknesses") or [],
            "ev_revenue_multiple": _num(x.get("ev_revenue_multiple")),
            "ev_ebitda_multiple": _num(x.get("ev_ebitda_multiple")),
            "recent_activity": _recent_activity(x.get("recent_activity")),
        })
    return _with_template("competitors", out)


def _recent_activity(rows):
    """Normalise recent_activity rows to the full Req 8 key set."""
    out = []
    for r in (rows or []):
        if not isinstance(r, dict):
            continue
        out.append({
            "activity_type": r.get("activity_type", "") or "",
            "date": r.get("date", "") or "",
            "investors_or_acquirers": r.get("investors_or_acquirers") or [],
            "target_company": r.get("target_company", "") or "",
            "deal_value_usd_mn": _num(r.get("deal_value_usd_mn")),
            "implied_valuation_multiple": _num(
                r.get("implied_valuation_multiple")),
        })
    return out


def _data_recent_news(sec, ctx):
    rows = [{
        "id": str(n.id),
        "title": n.headline or "",
        "date": (n.published_date.isoformat() if n.published_date else ""),
        "description": n.summary or "",
        "source": n.publisher or "",
        "link": n.url or "",
    } for n in ctx["news"]]
    return _with_template("recent_news", rows)


def _data_ai_company_summary(sec, ctx):
    return {"summary": (sec.content if sec else "") or ""}


def _investors(profile):
    from fundos.profile.models import Investor
    return Investor.objects.filter(profile=profile)


# Req 2 — the frontend renders these labels verbatim, so the API emits the
# display form rather than the internal enum.
_INVESTOR_TYPE_LABELS = {
    "VC": "Venture Capital (VC)",
    "PE": "Private Equity (PE)",
    "Angel": "Angel Investor",
    "Strategic": "Strategic Investor",
    "FamilyOffice": "Family Office",
}


def _data_cap_table(sec, ctx):
    """Req 2 — investors_and_cap_table.

    Shape is {cap_table_summary, investors_list}, not a bare array. Each row
    of investors_list is ONE INVESTMENT (investor x round), not one investor,
    because an investor participating in three rounds is three rows in the UI.
    """
    s = _obj(sec)
    investors = list(ctx.get("investors", []))

    # --- cap table summary ---
    stored = s.get("cap_table_summary") or {}
    ownership = []
    for row in (stored.get("ownership") or []):
        if isinstance(row, dict):
            ownership.append({
                "stakeholder_category": row.get("stakeholder_category", "")
                or row.get("holder_category", "") or "",
                "ownership_pct": row.get("ownership_pct"),
            })
    if not ownership:
        # Derive from investor rows when no explicit cap table was entered.
        buckets = {}
        for i in investors:
            cat = i.holder_category or "Investor"
            pct = _f(i.ownership_pct)
            if pct is not None:
                buckets[cat] = round(buckets.get(cat, 0) + pct, 4)
        ownership = [{"stakeholder_category": k, "ownership_pct": v}
                     for k, v in sorted(buckets.items())]

    total_investors = stored.get("total_investors")
    if total_investors is None:
        # Unique investor NAMES, not investment rows.
        total_investors = len({(i.name or "").strip().lower()
                               for i in investors if (i.name or "").strip()})

    # --- investors list: one row per investment ---
    rows = list(s.get("investors_list") or [])
    normalised = []
    for r in rows:
        if not isinstance(r, dict):
            continue
        normalised.append({
            "investor_name": r.get("investor_name", "") or "",
            "investor_type": r.get("investor_type", "") or "",
            "funding_amount_usd_mn": r.get("funding_amount_usd_mn"),
            "valuation_usd_mn": r.get("valuation_usd_mn"),
            "dilution_pct": r.get("dilution_pct"),
            "date": r.get("date", "") or "",
            "round": r.get("round", "") or "",
        })
    if not normalised:
        normalised = _investments_from_records(investors, ctx)

    return {
        "cap_table_summary": {
            "total_investors": total_investors,
            "ownership": ownership,
        },
        "investors_list": normalised,
    }


def _investments_from_records(investors, ctx):
    """Expand Investor x FundingRound into one row per investment.

    An investor who joined Seed and Series A appears twice, which is what the
    UI table shows. valuation_usd_mn is POST-money only (resolved in Req 2).
    """
    by_round = {}
    for r in ctx.get("funding_rounds", []):
        if r.round_name:
            by_round[r.round_name.strip().lower()] = r

    out = []
    for i in investors:
        label = _INVESTOR_TYPE_LABELS.get(i.investor_type or "",
                                          i.investor_type or "")
        rounds = i.rounds or []
        if not rounds:
            out.append({
                "investor_name": i.name or "", "investor_type": label,
                "funding_amount_usd_mn": None, "valuation_usd_mn": None,
                "dilution_pct": None, "date": "", "round": "",
            })
            continue
        for rn in rounds:
            fr = by_round.get(str(rn).strip().lower())
            out.append({
                "investor_name": i.name or "",
                "investor_type": label,
                # Per-investor cheque is rarely disclosed; the ROUND amount is
                # not a substitute and would overstate every investor, so it
                # stays null until entered.
                "funding_amount_usd_mn": None,
                "valuation_usd_mn": (
                    _f(getattr(fr, "post_money_value", None)) if fr else None),
                "dilution_pct": None,
                "date": (fr.announced_date.isoformat()
                         if fr and fr.announced_date else ""),
                "round": str(rn),
            })
    if not out:
        out = [{"investor_name": "", "investor_type": "",
                "funding_amount_usd_mn": None, "valuation_usd_mn": None,
                "dilution_pct": None, "date": "", "round": ""}]
    return out


def _obj(sec):
    return (sec.structured if sec else None) or {}


def _data_company_story(sec, ctx):
    """Req 5 — company_story_and_usp. `milestones` is new."""
    s = _obj(sec)
    milestones = []
    for m in (s.get("milestones") or []):
        if isinstance(m, dict):
            milestones.append({"date": m.get("date", "") or "",
                               "title": m.get("title", "") or "",
                               "description": m.get("description", "") or ""})
        elif isinstance(m, str) and m.strip():
            # Tolerate a bare string from generation rather than dropping it.
            milestones.append({"date": "", "title": m.strip(),
                               "description": ""})
    return {"origin_story": s.get("origin_story", "") or "",
            "brand_evolution": s.get("brand_evolution", "") or "",
            "milestones": milestones,
            "usp": s.get("usp", "") or ""}


def _data_market_research(sec, ctx):
    """Req 6, extended for CR-11.

    A bare TAM/SAM/SOM number is unverifiable and therefore untrustworthy —
    the reader cannot tell a sourced estimate from an invention. Each figure
    now travels with the source it came from and the assumption behind it,
    and the section carries a narrative writeup explaining the derivation.
    Empty strings are emitted rather than omitted so the client always shows
    the reader WHERE a number is missing its provenance.
    """
    s = _obj(sec)
    ms = s.get("market_sizing") or {}

    def _figure(key):
        raw = ms.get(key)
        # Accept both the legacy scalar form and the new object form, so
        # profiles generated before this change keep rendering.
        if isinstance(raw, dict):
            return {"value": raw.get("value", "") or "",
                    "source": raw.get("source", "") or "",
                    "assumptions": raw.get("assumptions", "") or ""}
        return {"value": raw if raw is not None else "",
                "source": "", "assumptions": ""}

    return {"industry_evolution": s.get("industry_evolution", "") or "",
            # CR-11: the paragraph-format writeup. Numbers alone are not
            # presented without this context.
            "market_sizing_narrative": _first_present(
                s, "market_sizing_narrative", "market_sizing_writeup",
                default="") or "",
            "methodology": s.get("methodology", "") or "",
            "market_sizing": {"tam": _figure("tam"), "sam": _figure("sam"),
                              "som": _figure("som")},
            "performance_trends": s.get("performance_trends") or [],
            "regulatory_developments": s.get("regulatory_developments") or []}


def _data_investment_thesis(sec, ctx):
    """Req 7. Wire keys are opportunity_explanation / leadership_assessment /
    risks_and_concerns. Older storage keys (why_attractive,
    risks_and_open_questions) are still READ so existing profiles and the
    Judgement-tier output keep working without a data migration."""
    s = _obj(sec)
    return {
        "opportunity_explanation": _first_present(
            s, "opportunity_explanation", "why_attractive", default="") or "",
        "leadership_assessment": s.get("leadership_assessment", "") or "",
        "risks_and_concerns": (s.get("risks_and_concerns")
                               or s.get("risks_and_open_questions") or []),
    }


# A person block inside leadership_detail. Every key is always present and
# empty rather than the whole block being sent as a bare `{}`: an empty object
# carries no field structure for the client to render, and a generic key/value
# renderer handed `{}` has nothing to show. This mirrors how the list sections
# send a template row (_LIST_ROW_TEMPLATES) when they have no data.
_LEADERSHIP_PERSON_FIELDS = ("name", "role", "background", "tenure",
                             "linkedin_url")


def _leadership_person(value):
    """Normalise a person block to the full key set."""
    src = value if isinstance(value, dict) else {}
    return {f: (src.get(f) or "") for f in _LEADERSHIP_PERSON_FIELDS}


def _data_leadership_detail(sec, ctx):
    """CFO first (requirement §4), then founders, then CEO."""
    s = _obj(sec)
    founders = [_leadership_person(f) for f in (s.get("founders") or [])
                if isinstance(f, dict)]
    if not founders:
        # Fall back to the real founder rows so the section is not blank
        # when the AI has not written its own leadership block yet.
        founders = [{
            "name": f.name or "",
            "role": f.designation or "",
            "background": (f.experience or f.biography or ""),
            "tenure": "",
            "linkedin_url": f.linkedin_url or "",
        } for f in ctx["founders"]]
    if not founders:
        founders = [_leadership_person(None)]
    return {"cfo": _leadership_person(s.get("cfo")),
            "founders": founders,
            "ceo": _leadership_person(s.get("ceo")),
            "org_structure_note": s.get("org_structure_note", "") or ""}


_DERIVED_MULTIPLE_ROW = {"financial_year": None, "ev_usd_mn": None,
                         "ev_revenue_multiple": None,
                         "ev_ebitda_multiple": None}


def _data_derived_multiples(sec, ctx):
    s = _obj(sec)
    rows = []
    for r in (s.get("by_year") or []):
        if not isinstance(r, dict):
            continue
        row = dict(_DERIVED_MULTIPLE_ROW)
        row.update({k: r.get(k) for k in _DERIVED_MULTIPLE_ROW if k in r})
        rows.append(row)
    if not rows:
        rows = [dict(_DERIVED_MULTIPLE_ROW)]
    return {"ev_basis": s.get("ev_basis", "") or "",
            "by_year": rows}


_BUILDERS = {
    "company_profile": _data_company_profile,
    "founders": _data_founders,
    "products_services": _data_products_services,
    "customers_markets": _data_customers_markets,
    "competitive_advantages": _data_competitive_advantages,
    "business_model": _data_business_model,
    "revenue_model": _data_revenue_model,
    "company_metrics": _data_company_metrics,
    "financial_summary": _data_financial_summary,
    "funding_history": _data_funding_history,
    "competitors": _data_competitors,
    # Generated-profile contract names. The four renamed sections keep their
    # old wire names as aliases below, so a client mid-migration is not broken
    # by the rename alone.
    "news": _data_recent_news,
    "investors_cap_table": _data_cap_table,
    "company_story": _data_company_story,
    "industry_research": _data_market_research,
    "investment_thesis": _data_investment_thesis,
    "document_center": _data_document_center,

    # --- retired, still served if an admin re-activates the section ------
    # These are no longer part of the generated contract (see
    # seed_platform_config._section_schema), but the builders are kept so
    # re-activating a section row in admin actually restores it rather than
    # raising a KeyError on the next GET.
    "key_people": _data_key_people,
    "ai_company_summary": _data_ai_company_summary,
    "leadership_detail": _data_leadership_detail,
    "derived_multiples": _data_derived_multiples,
    # Previous wire names for the four renamed sections.
    "recent_news": _data_recent_news,
    "investors_and_cap_table": _data_cap_table,
    "company_story_and_usp": _data_company_story,
    "industry_and_market_research": _data_market_research,
}


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------
def _structured_list(sec, key):
    """Pull a list out of a section's structured JSON.

    Structured list-style sections may store their rows either directly as a
    list, or under an ``items`` key. Be tolerant of both.
    """
    if not sec:
        return []
    st = sec.structured or {}
    if isinstance(st, list):
        return st
    val = st.get(key)
    if isinstance(val, list):
        return val
    # Some sections store the list under the section's own key.
    for v in st.values():
        if isinstance(v, list):
            return v
    return []


def _split_list(value):
    if not value:
        return []
    if isinstance(value, list):
        return value
    return [p.strip() for p in str(value).split(",") if p.strip()]


def _content_note(sec):
    """Prose the AI stored in ProfileSection.content, used as a fallback for
    sections whose structured data is empty (Gap2 content-migration finding).
    """
    if not sec:
        return ""
    text = (sec.content or "").strip()
    # Skip mock placeholders that carry no real finding.
    return text


# Object sections whose isComplete should reflect that the CORE fields are
# filled, not merely that any one field has a value (issue #4). A section is
# complete when all of its core fields are non-empty.
_OBJECT_CORE_FIELDS = {
    "company_profile": [
        "description_of_business", "macro_sector", "sub_sector",
        "funding_status_name", "revenue_size_name",
    ],
    "business_model": [
        "customer_type", "value_proposition", "delivery_model",
        "pricing_model", "sales_model",
    ],
}


def _section_is_complete(spec_key, data, sec, required_keys):
    """Per-section completeness.

    * Object sections with defined core fields (company_profile,
      business_model) are complete only when ALL core fields are filled —
      fixes issue #4 where company_profile.isComplete was true with 6/9
      fields blank.
    * financial_summary is complete when it has any financials row.
    * List sections are complete when they hold at least one row.
    * All sections additionally respect an explicit needs_input flag.
    """
    if sec is not None and sec.needs_input:
        return False

    if spec_key in _OBJECT_CORE_FIELDS:
        if not isinstance(data, dict):
            return False
        return all(str(data.get(f, "") or "").strip()
                   for f in _OBJECT_CORE_FIELDS[spec_key])

    if spec_key == "financial_summary":
        if not isinstance(data, dict):
            return False
        # The financials array always carries at least a null placeholder row
        # (self-describing empty state); it counts as complete only when a row
        # holds a real value, or there is at least one observation.
        rows = data.get("financials") or []
        return (any(_row_has_value(r) for r in rows)
                or bool(data.get("observations")))

    return _has_data(data)


def _has_data(data):
    if isinstance(data, list):
        # A list counts as having data only if at least one row has a
        # non-empty value. This makes the empty-field TEMPLATE row (all
        # blanks) correctly read as "no data", so a section showing only a
        # template stays isComplete:false.
        return any(_row_has_value(row) for row in data)
    if isinstance(data, dict):
        for v in data.values():
            # Nested lists get the same treatment as top-level ones: a list
            # holding only a blank template row is not data. Without this,
            # sections whose template includes a placeholder row (e.g.
            # leadership_detail.founders, derived_multiples.by_year) reported
            # isComplete:true on a brand-new, entirely empty profile.
            if isinstance(v, list):
                if any(_row_has_value(row) for row in v):
                    return True
                continue
            if isinstance(v, dict):
                if _has_data(v):
                    return True
                continue
            if isinstance(v, str) and v.strip():
                return True
            if isinstance(v, bool):
                if v:
                    return True
            elif isinstance(v, (int, float)) and v:
                return True
        return False
    return bool(data)


def _row_has_value(row):
    """True if a list row carries any real (non-empty, non-zero, non-false)
    value. Used so an all-blank template row reads as no data."""
    if not isinstance(row, dict):
        return bool(row)
    for v in row.values():
        if isinstance(v, str) and v.strip():
            return True
        # Recurse rather than truth-testing the container: a person block
        # like {"name": "", "role": ""} is non-empty as a dict but carries
        # no actual value, and treating it as data made empty sections
        # report complete.
        if isinstance(v, dict):
            if _row_has_value(v):
                return True
            continue
        if isinstance(v, list):
            if any(_row_has_value(x) for x in v):
                return True
            continue
        if isinstance(v, bool):
            # A boolean is NEVER evidence that a row carries data. Every row
            # has one, true or false, so a `True` default is indistinguishable
            # from a founder someone actually entered — which is how adding
            # `is_founder: True` to the founders template briefly made an
            # empty profile report that section complete. Booleans qualify a
            # row; they do not populate it.
            continue
        if isinstance(v, (int, float)) and v:
            return True
    return False


def _extraction_map(documents):
    """material_asset_id -> how that file was actually read.

    One query for every document, rather than one per document inside the
    Document Center builder. Returns an empty map on any failure: the
    extraction columns are additive and an unmigrated database must degrade to
    "we do not know how this was read", not to a broken profile page.
    """
    ids = [d.material_asset_id for d in documents if d.material_asset_id]
    if not ids:
        return {}
    try:
        from fundos.readiness.models import MaterialAsset
        rows = MaterialAsset.objects.filter(id__in=ids).values(
            "id", "extraction_status", "handler", "ocr_status", "ocr_reason")
    except Exception:
        return {}
    return {r["id"]: {"status": r["extraction_status"],
                      "handler": r["handler"],
                      "ocr_status": r["ocr_status"],
                      "ocr_reason": r["ocr_reason"]} for r in rows}


def build_sections(profile):
    """Return the spec §8 ``sections`` object keyed by spec section key."""
    from fundos.platformcfg.services import completeness_section_keys
    from fundos.profile.models import (
        Competitor, Founder, FundingRound, KeyPerson, NewsItem,
        ProfileDocument, ProfileSection,
    )

    # Prefetch related rows once.
    ctx = {
        "profile": profile,
        "founders": list(Founder.objects.filter(profile=profile)),
        "key_people": list(KeyPerson.objects.filter(profile=profile)),
        "competitors": list(Competitor.objects.filter(profile=profile)),
        "funding_rounds": list(FundingRound.objects.filter(profile=profile)),
        "news": list(NewsItem.objects.filter(profile=profile)),
        "investors": list(_investors(profile)),
        "documents": list(ProfileDocument.objects.filter(profile=profile)),
    }
    ctx["extraction"] = _extraction_map(ctx["documents"])

    # Map every stored section row by its SPEC key for quick lookup.
    rows = {}
    for sec in ProfileSection.objects.filter(profile=profile, is_active=True):
        spec_key = INTERNAL_TO_SPEC_KEY.get(sec.section_key, sec.section_key)
        spec_key = _WIRE_FOR_STORAGE.get(spec_key, spec_key)
        # Five sections are reachable under two names — the wire key and the
        # storage key — so two rows can map here to one section. An empty one
        # must never displace a row carrying real work, whichever order the
        # database returns them in.
        kept = rows.get(spec_key)
        if kept is not None and not _row_has_content(sec):
            continue
        rows[spec_key] = sec

    required = set(
        INTERNAL_TO_SPEC_KEY.get(k, k) for k in completeness_section_keys())
    required = {_WIRE_FOR_STORAGE.get(k, k) for k in required}

    sections = {}
    for spec_key in spec_section_order():
        sec = rows.get(spec_key)
        builder = _BUILDERS.get(spec_key)
        if builder is None:
            # An admin has activated a section with no builder. Emitting an
            # empty block is better than a 500 on every profile read, and the
            # empty `data` says plainly that nothing is served for it.
            sections[spec_key] = {
                "sectionKey": spec_key, "isComplete": False,
                "lastUpdatedAt": _iso(sec.updated_at) if sec else None,
                "data": {} if spec_key in _OBJECT_SECTIONS else []}
            continue
        data = builder(sec, ctx)
        sections[spec_key] = {
            "sectionKey": spec_key,
            "isComplete": _section_is_complete(spec_key, data, sec, required),
            "lastUpdatedAt": _iso(sec.updated_at) if sec else None,
            "confirmed_fields": (sec.confirmed_fields
                                 if sec and hasattr(sec, 'confirmed_fields')
                                 else []),
            "data": data,
        }
    return sections


def serialize_section(profile, spec_key):
    """Serialize ONE section in the §8 wrapper, matching the GET shape.

    Used to echo a section back after a PATCH so the client can confirm the
    write without a follow-up GET.
    """
    from fundos.profile.models import (
        Competitor, Founder, FundingRound, KeyPerson, NewsItem,
        ProfileDocument, ProfileSection,
    )

    if spec_key not in _BUILDERS:
        raise KeyError(spec_key)

    ctx = {
        "profile": profile,
        "founders": list(Founder.objects.filter(profile=profile)),
        "key_people": list(KeyPerson.objects.filter(profile=profile)),
        "competitors": list(Competitor.objects.filter(profile=profile)),
        "funding_rounds": list(FundingRound.objects.filter(profile=profile)),
        "news": list(NewsItem.objects.filter(profile=profile)),
        "investors": list(_investors(profile)),
        "documents": list(ProfileDocument.objects.filter(profile=profile)),
    }
    ctx["extraction"] = _extraction_map(ctx["documents"])
    internal_key = storage_key_for(spec_key)
    sec = ProfileSection.objects.filter(
        profile=profile, section_key=internal_key, is_active=True).first()
    data = _BUILDERS[spec_key](sec, ctx)
    return {
        "sectionKey": spec_key,
        "isComplete": _section_is_complete(spec_key, data, sec, set()),
        "lastUpdatedAt": _iso(sec.updated_at) if sec else None,
        "confirmed_fields": (sec.confirmed_fields
                             if sec and hasattr(sec, 'confirmed_fields')
                             else []),
        "data": data,
    }


# Section weights for readiness scoring (Req 4).  Core sections that
# carry the most deal-relevant information get weight 2; supporting
# sections get weight 1.  Adding a section here makes it count toward
# the readiness score; omitting it keeps it visible but unscored.
_SECTION_WEIGHTS = {
    "company_profile": 2.0,
    "founders": 2.0,
    "products_services": 2.0,
    "customers_markets": 2.0,
    "competitive_advantages": 1.0,
    "business_model": 2.0,
    "revenue_model": 2.0,
    "company_metrics": 2.0,
    "financial_summary": 1.0,
    "funding_history": 1.0,
    "competitors": 1.0,
    "news": 1.0,
    "document_center": 1.0,
    "investors_cap_table": 1.0,
    "company_story": 1.0,
    "industry_research": 1.0,
    "investment_thesis": 1.0,
}


#: The key an item in a list section is confirmed BY. Assigned when the item
#: is written (see `section_writer._ensure_item_ids`) so a confirmation stays
#: attached to the founder it was given to, rather than to whatever ends up in
#: that position after the next research run.
ITEM_ID = "id"


def _confirmed_items(section_key, data, confirmed):
    """How many objects in a list section a human has actually confirmed.

    Was `len(data) if cf else 0` — one click confirmed every founder in the
    list, including the six nobody had read. An object is one part with one
    Confirm, so this counts the parts that were confirmed and no others.

    Three keys address the same object, and any of them counts it once:

    * its `id` — the durable one, and the only one worth sending;
    * its position as a string, "0".."N" — what clients sent before items
      carried ids, and what they still send today;
    * the section key itself — the whole-section sentinel, from when
      confirming was all-or-nothing.

    The last two are read, never written. Reading a position as zero silently
    discarded confirmations people had already made: five founders confirmed,
    `confirmed_fields: ["0","1","2","3","4"]` stored, and a breakdown that
    said `confirmed: 0` — the work was done and the score never moved.

    A position is a weak address: the list comes back from the database in no
    guaranteed order, so "2" may name a different founder after the next
    research run, and an id never does. It is honoured because dropping it
    loses real confirmations, not because it is sound. Counting is per item,
    so an item addressed by both its id and its position is still one.
    """
    if not confirmed:
        return 0
    if section_key in confirmed:
        return len(data)
    return sum(1 for position, item in enumerate(data)
               if isinstance(item, dict)
               and (str(item.get(ITEM_ID) or "") in confirmed
                    or str(position) in confirmed))


def _row_has_content(sec):
    """Does this section row carry anything worth preferring?"""
    return bool(sec and (sec.content or sec.structured or
                         sec.confirmed_fields))


def _readiness_breakdown(sections_dict):
    """Per-section readiness statistics (Req 4 audit trail).

    Returns a list of {sectionKey, fields, populated, confirmed, weight}
    objects — one per section in the payload — so the frontend (and any
    auditor) can see exactly how the top-level score was derived.
    """
    from fundos.profile import schema as profile_schema

    spec = profile_schema.sections_by_key()

    breakdown = []
    for key, sec in sections_dict.items():
        data = sec.get("data", {})
        cf = set(sec.get("confirmed_fields") or [])
        weight = _SECTION_WEIGHTS.get(key, 1.0)

        if isinstance(data, dict):
            # COUNT WHAT THE SCREEN SHOWS, not what the payload carries.
            #
            # The count used to come from `data.keys()`, which includes keys
            # the form never renders — `total_funding_raised_display` beside
            # `total_funding_raised_usd_mn`, and the same for pre- and
            # post-money. Those three are not fields, they are the currency
            # and unit halves of a control the user sees as ONE input, so
            # `company_profile` counted 15 where the screen offers 12.
            #
            # They could never be confirmed either, because no control exists
            # to confirm them — so every profile carried three fields that
            # were permanently unconfirmable and permanently dragging the
            # score down: 10/15 = 67% for a section a founder had in fact
            # completed to 10/12 = 83%.
            #
            # The schema IS the form, so it is the honest denominator.
            declared = list((spec.get(key) or {}).get("fields") or {})

            # A section that is really a WRAPPER around a list counts the
            # list. `document_center` declares one field, `documents`, whose
            # value is the uploaded files — so it reported `fields: 1` for a
            # company that had uploaded nine, and one confirmation would have
            # covered every document in the pack. One object is one part
            # whether the array sits at the top of the section or one key in.
            if len(declared) == 1 and isinstance(data.get(declared[0]), list):
                items = data[declared[0]]
                field_keys = max(len(items), 1)
                populated = len(items)
                confirmed = _confirmed_items(key, items, cf)
                breakdown.append({
                    "sectionKey": key, "fields": field_keys,
                    "populated": populated, "confirmed": confirmed,
                    "weight": weight,
                })
                continue

            field_keys = declared or list(data.keys())
            populated = sum(
                1 for k in field_keys
                if data.get(k) not in (None, "", [], {}))
            confirmed = len(cf & set(field_keys))
        elif isinstance(data, list):
            # One object is one part. A founder is a single unit with a
            # single Confirm — the name, role and background inside it are
            # never counted separately.
            field_keys = max(len(data), 1)
            populated = len(data)
            confirmed = _confirmed_items(key, data, cf)
        else:
            field_keys = 0
            populated = 0
            confirmed = 0

        breakdown.append({
            "sectionKey": key,
            "fields": field_keys if isinstance(field_keys, int) else len(field_keys),
            "populated": populated,
            "confirmed": confirmed,
            "weight": weight,
        })
    return breakdown


def _readiness_totals(breakdown):
    """The whole profile in three numbers, so the ring needs no arithmetic.

    Every consumer wants the same "0 of 79 verified" caption and every one of
    them was computing it by summing the breakdown itself — which is how the
    readiness ring came to show a headline of 12 beside a caption of 0/77 on
    the same screen, each half derived a different way. One total, computed
    where the score is computed, leaves nothing to disagree about.

    `fields` is the honest denominator: it is what the score divides by, so a
    caption built on `populated` reads as a higher fraction of the work than
    the score will ever credit.
    """
    return {
        "fields": sum(e["fields"] for e in breakdown),
        "populated": sum(e["populated"] for e in breakdown),
        "confirmed": sum(e["confirmed"] for e in breakdown),
        "sections": len(breakdown),
        "sectionsConfirmed": sum(
            1 for e in breakdown
            if e["fields"] and e["confirmed"] >= e["fields"]),
    }


def _readiness_score(sections_dict):
    """Compute readiness score from confirmed fields (Req 4).

    Only fields that appear in a section's confirmed_fields count toward
    the score. AI-drafted but unconfirmed fields contribute 0%.
    The score is weighted by section importance.
    """
    breakdown = _readiness_breakdown(sections_dict)
    total_weight = 0.0
    earned_weight = 0.0
    for entry in breakdown:
        w = entry["weight"]
        fields = entry["fields"]
        if fields == 0:
            continue
        total_weight += w
        earned_weight += w * (entry["confirmed"] / fields)
    if total_weight == 0:
        return 0
    return round(earned_weight / total_weight * 100)


def _readiness_stage(score):
    """Map score 0–100 to the readiness ladder label (Req 4)."""
    if score >= 95:
        return "investor-ready"
    if score >= 90:
        return "nearly there"
    if score >= 70:
        return "well underway"
    if score >= 45:
        return "taking shape"
    return "just getting started"


def serialize_profile(profile, company, *, default_deal_id=None,
                      ai_mocked=False):
    """Full §6 top-level record with the §8 sections object."""
    sections = build_sections(profile)
    breakdown = _readiness_breakdown(sections)
    score = _readiness_score(sections)
    return {
        "companyId": str(company.id),
        "companyName": company.name,
        "defaultDealId": str(default_deal_id) if default_deal_id else None,
        "websiteUrl": profile.website_url,
        "hqCountry": profile.hq_country,
        "logoUrl": profile.logo_url or None,
        # CR-01 — the company's own LinkedIn page.
        "linkedinUrl": profile.linkedin_url or "",
        "homeCurrency": profile.home_currency,
        "status": profile.status,
        "completenessPct": _f(profile.completeness_pct) or 0.0,
        "isComplete": profile.is_complete,
        "score": score,
        "readiness_stage": _readiness_stage(score),
        "readinessBreakdown": breakdown,
        "readinessTotals": _readiness_totals(breakdown),
        "reviewedAt": _iso(profile.reviewed_at),
        "lastGeneratedAt": _iso(profile.last_generated_at),
        "aiMocked": ai_mocked,
        "sections": sections,
    }
