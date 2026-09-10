"""
LLM response normalization (LLM_ISSUE2: "success but empty" fix).

Real providers frequently return valid JSON whose *shape* differs from the
exact keys our downstream code reads — the call logs `success`, but the
content is silently dropped and the section saves empty. The four Company
Profile roles have no strict schema, so a wrong-shaped-but-valid response
passes straight through.

This module coerces whatever reasonable JSON a model returns into the
canonical shape each role's consumer expects, so generation is tolerant of
provider/prompt drift without any prompt change. It runs in the adapter,
after JSON parse/repair and before role dispatch back to the caller — so it
covers both the primary and fallback endpoints uniformly.

Canonical shapes the code consumes:
  * company_profile_section / founder narrative → {"content": str,
        "needsInput": bool, "missing": [str]}
  * company_profile_records (list sections)     → {"records": [ {...} ]}
  * company_profile_records (numeric forms)     → {"structured": {"items":[...]}}
  * company_profile_records (financial_summary) → {"structured":
        {"items":[...], "observations":[...]}}
  * company_profile_structured                  → {"items":[...]} or
        {"object": {...}} (+ optional content)

Normalization is CONSERVATIVE: if the canonical key is already present and
non-empty it is left untouched; we only fill it from a recognised alternate
when it is missing/empty. Numbers are never invented — we only move values
the model already returned into the expected slot.
"""

# Alternate keys a model may use for narrative prose, in priority order.
_NARRATIVE_KEYS = ("content", "summary", "text", "body", "narrative",
                   "section_content", "prose", "description", "output",
                   "answer", "result")

# Alternate keys for a list of records.
_RECORD_LIST_KEYS = ("records", "items", "data", "rows", "list", "entries",
                     "results")

# Per-section natural-language keys a model may nest the list under, e.g.
# {"key_people": [...]} instead of {"records": [...]}.
_SECTION_LIST_ALIASES = {
    "key_people": ("key_people", "keyPeople", "people", "team"),
    "competitors": ("competitors", "competition", "competitor_list"),
    "funding_history": ("funding_history", "fundingHistory", "funding",
                        "rounds", "funding_rounds"),
    "recent_news": ("recent_news", "recentNews", "news", "articles"),
    "founders": ("founders", "founder_list"),
    "products_services": ("products_services", "products", "services",
                          "products_and_services"),
    "customers_markets": ("customers_markets", "customers", "markets",
                          "customer_segments"),
    "competitive_advantages": ("competitive_advantages", "advantages",
                               "moats", "differentiators"),
    "revenue_model": ("revenue_model", "revenue_streams", "streams"),
    "company_metrics": ("company_metrics", "metrics", "kpis"),
}


def _first_nonempty(d, keys):
    for k in keys:
        if k in d and d[k] not in (None, "", [], {}):
            return d[k]
    return None


def normalize(role: str, data, *, section_key: str = ""):
    """Return `data` coerced to the canonical shape for `role`.

    Never raises — on anything unexpected it returns the input unchanged, so
    normalization can only help, never break a call.
    """
    try:
        if role in ("company_profile_section", "founder_profile"):
            return _normalize_narrative(data)
        if role == "company_profile_records":
            return _normalize_records(data, section_key)
        if role == "company_profile_structured":
            return _normalize_structured(data, section_key)
    except Exception:
        return data
    return data


def _normalize_narrative(data):
    # A bare string response → wrap as content.
    if isinstance(data, str):
        return {"content": data, "needsInput": not data.strip(), "missing": []}
    if not isinstance(data, dict):
        return data
    out = dict(data)
    if not (out.get("content") or out.get("summary")):
        prose = _first_nonempty(out, _NARRATIVE_KEYS)
        if isinstance(prose, list):
            # Some models return paragraphs as a list of strings.
            prose = "\n\n".join(str(p) for p in prose if p)
        if isinstance(prose, str) and prose.strip():
            out["content"] = prose
    if "needsInput" not in out:
        out["needsInput"] = not bool(out.get("content"))
    out.setdefault("missing", [])
    return out


def _normalize_records(data, section_key):
    # A bare list → wrap under records.
    if isinstance(data, list):
        return {"records": data}
    if not isinstance(data, dict):
        return data
    out = dict(data)

    # For numeric-form sections the consumer reads structured.items; for
    # financial_summary it also reads structured.observations.
    numeric_like = section_key in (
        "revenue_model", "company_metrics", "financial_summary")

    if numeric_like:
        structured = out.get("structured")
        if not isinstance(structured, dict):
            structured = {}
        if not structured.get("items"):
            items = _find_record_list(out, section_key)
            if items is not None:
                structured["items"] = items
        # financial_summary: lift top-level financials/observations in.
        if section_key == "financial_summary":
            if not structured.get("items"):
                fin = _first_nonempty(out, ("financials", "financial_summary",
                                            "fiscal_years", "years"))
                if isinstance(fin, list):
                    structured["items"] = fin
            if not structured.get("observations"):
                obs = _first_nonempty(out, ("observations", "notes",
                                            "commentary", "highlights"))
                if isinstance(obs, list):
                    structured["observations"] = obs
        if structured:
            out["structured"] = structured
        return out

    # List-record sections: ensure a top-level `records` list.
    if not out.get("records"):
        records = _find_record_list(out, section_key)
        if records is not None:
            out["records"] = records
    return out


def _find_record_list(d, section_key):
    """Locate the list of record dicts among the common/aliased keys."""
    # 1) generic list keys
    found = _first_nonempty(d, _RECORD_LIST_KEYS)
    if isinstance(found, list):
        return found
    # 2) section-specific natural-language keys
    for alias in _SECTION_LIST_ALIASES.get(section_key, ()):  # noqa
        val = d.get(alias)
        if isinstance(val, list):
            return val
    # 3) a single top-level list value, if unambiguous
    lists = [v for v in d.values() if isinstance(v, list)]
    if len(lists) == 1:
        return lists[0]
    return None


def _normalize_structured(data, section_key=""):
    # company_profile_structured → {"items":[...]} or {"object":{...}}
    # (+ optional "content"). business_model is the object-shaped one; the
    # other three are list-shaped.
    if isinstance(data, list):
        return {"items": data}
    if not isinstance(data, dict):
        return data
    out = dict(data)

    if section_key == "business_model":
        if not isinstance(out.get("object"), dict):
            obj = _first_nonempty(out, ("object", "fields", "data",
                                        "business_model", "model"))
            if isinstance(obj, dict):
                out["object"] = obj
            else:
                # Model returned the fields flat at the top level — lift the
                # known business_model fields into object.
                bm_keys = ("customer_type", "value_proposition",
                           "delivery_model", "pricing_model", "sales_model",
                           "distribution_channels")
                flat = {k: out[k] for k in bm_keys if k in out}
                if flat:
                    out["object"] = flat
        return out

    # List-shaped structured sections.
    if not out.get("items"):
        items = _find_record_list(out, section_key)
        if isinstance(items, list):
            out["items"] = items
    return out
