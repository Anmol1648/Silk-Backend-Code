"""A change the chat suggests, and the one way it can be applied.

The Q&A box could always write a better description; it could never save
one. A founder read the answer and retyped it into the form. So the chat
now returns its suggestion as a PROPOSAL -- the section, the field, the text
that is there now, and the text it suggests -- and a person decides.

Three rules hold this together.

    NOTHING APPLIES ITSELF. A proposal is a suggestion until someone clicks.
    A chat that silently edits a profile is how a founder's confirmed field
    gets replaced by a paraphrase nobody approved.

    A PROPOSAL IS VALIDATED BEFORE IT IS SHOWN. The section and the field
    must exist in the schema and the field must be a plain text one. A model
    naming a field that does not exist is dropped rather than displayed --
    an invention that looks right survives review precisely because it looks
    right.

    `currentValue` COMES FROM THE PROFILE, never from the model. The diff a
    founder is shown has to be a real diff; a model that misremembers the
    present text must not be able to fake one.

TEXT AND LIST ITEM FIELDS: Supported across all object and list sections.
Supports actions: "edit" (default), "add" (new list item), "delete" (remove item).
Array items (founders, competitors, products, metrics, etc.) are matched by itemId,
itemName, itemIndex, or contextual text in the prompt/reason.
"""
import logging
import re
import uuid

logger = logging.getLogger(__name__)

#: A field spec the model is given describes the type in its first word.
_TEXT_SPEC_PREFIX = "string"

#: Fields that are text by type but are NOT free text: each is snapped onto a
#: controlled vocabulary elsewhere, and a chat rewrite would quietly unmatch
#: the join key the benchmark cohort is selected by.
CONTROLLED_FIELDS = {
    ("company_profile", "sub_sector"),
    ("company_profile", "macro_sector"),
    ("company_profile", "funding_status_name"),
    ("company_profile", "revenue_size_name"),
    ("company_profile", "currency_id"),
    ("company_profile", "country"),
}

#: A proposal longer than this is not an edit, it is a new document.
MAX_VALUE_CHARS = 20_000

FIELD_ALIASES = {
    "latest_pre_money_display": "latest_pre_money_usd_mn",
    "latest_post_money_display": "latest_post_money_usd_mn",
    "total_funding_raised_display": "total_funding_raised_usd_mn",
    "latest_pre_money_amount": "latest_pre_money_usd_mn",
    "latest_post_money_amount": "latest_post_money_usd_mn",
    "total_funding_raised_amount": "total_funding_raised_usd_mn",
    "metric_name": "metric",
    "kpi_name": "metric",
    "kpi": "metric",
}

LIST_SECTIONS = {
    "founders", "products_services", "customers_markets", "competitive_advantages",
    "revenue_model", "company_metrics", "funding_history", "competitors",
    "news", "investors_cap_table"
}

SECTION_ALIASES = {
    "metrics": "company_metrics",
    "company_metric": "company_metrics",
    "metric": "company_metrics",
    "kpis": "company_metrics",
    "kpi": "company_metrics",
    "founders_and_key_people": "founders",
    "founder": "founders",
    "key_people": "founders",
    "team": "founders",
    "leadership": "founders",
    "products_and_services": "products_services",
    "products": "products_services",
    "services": "products_services",
    "product": "products_services",
    "service": "products_services",
    "customers_and_markets": "customers_markets",
    "customers": "customers_markets",
    "markets": "customers_markets",
    "target_market": "customers_markets",
    "target_markets": "customers_markets",
    "competitive_advantage": "competitive_advantages",
    "advantages": "competitive_advantages",
    "usps": "competitive_advantages",
    "usp": "competitive_advantages",
    "business_models": "business_model",
    "revenue_models": "revenue_model",
    "revenue_streams": "revenue_model",
    "revenue": "revenue_model",
    "financials": "financial_summary",
    "financial_highlights": "financial_summary",
    "funding": "funding_history",
    "fundraising": "funding_history",
    "funding_rounds": "funding_history",
    "rounds": "funding_history",
    "competitor": "competitors",
    "competition": "competitors",
    "recent_news": "news",
    "press": "news",
    "cap_table": "investors_cap_table",
    "investors": "investors_cap_table",
    "company_overview": "company_profile",
    "overview": "company_profile",
    "documents": "document_center",
}


def _norm_str(s):
    return re.sub(r'[\s\-_]+', ' ', str(s or '').strip()).lower()


def resolve_section_key(section_key):
    """Dynamically resolve wire key for any section key or alias."""
    raw = str(section_key or "").strip()
    if not raw:
        return ""
    norm = re.sub(r'[\s\-]+', '_', raw.lower())

    if norm in SECTION_ALIASES:
        return SECTION_ALIASES[norm]

    from fundos.profile.schema import SHIPPED_SECTIONS, sections
    try:
        sec_list = sections() or SHIPPED_SECTIONS
    except Exception:
        sec_list = SHIPPED_SECTIONS

    for sec in sec_list:
        if isinstance(sec, dict):
            k = sec.get("key")
            sk = sec.get("storage_key")
            if k and (k == raw or k == norm or _norm_str(k) == _norm_str(norm)):
                return k
            if sk and (sk == raw or sk == norm or _norm_str(sk) == _norm_str(norm)):
                return k or sk

    return SECTION_ALIASES.get(norm, norm)


def is_list_section(section_key):
    """Check if section stores array/list of items dynamically."""
    canon = resolve_section_key(section_key)
    if canon in LIST_SECTIONS:
        return True

    from fundos.profile.schema import SHIPPED_SECTIONS, sections
    try:
        sec_list = sections() or SHIPPED_SECTIONS
    except Exception:
        sec_list = SHIPPED_SECTIONS

    for sec in sec_list:
        if isinstance(sec, dict) and (sec.get("key") == canon or sec.get("storage_key") == canon):
            kind = str(sec.get("kind") or sec.get("container_kind") or "").lower()
            return kind in ("array", "list") or sec.get("is_array") is True

    return False


def editable_fields():
    """``{(section_key, field)}`` the chat may propose a change to across object sections."""
    from fundos.profile.schema import sections

    out = set()
    for section in sections():
        key = section["key"]
        if is_list_section(key):
            continue
        for field, spec in (section.get("fields") or {}).items():
            if not str(spec).startswith(_TEXT_SPEC_PREFIX):
                continue
            if (key, field) in CONTROLLED_FIELDS:
                continue
            out.add((key, field))
    return out


def _evidence(raw):
    """Whatever the model cited for this change, as a list of strings."""
    if isinstance(raw, str):
        return [raw] if raw.strip() else []
    if not isinstance(raw, list):
        return []
    out = []
    for item in raw[:5]:
        if isinstance(item, str) and item.strip():
            out.append(item.strip())
        elif isinstance(item, dict):
            source = str(item.get("source") or "").strip()
            locator = str(item.get("locator") or "").strip()
            if source:
                out.append(f"{source} — {locator}" if locator else source)
    return out


def _section_fields(section_key):
    """Dynamically fetch field keys for a section from schema.sections()."""
    canon = resolve_section_key(section_key)
    from fundos.profile.schema import SHIPPED_SECTIONS, sections
    try:
        sec_list = sections() or SHIPPED_SECTIONS
    except Exception:
        sec_list = SHIPPED_SECTIONS

    for sec in sec_list:
        if isinstance(sec, dict) and (sec.get("key") == canon or sec.get("storage_key") == canon):
            fields = sec.get("fields") or {}
            if isinstance(fields, dict):
                return set(fields.keys())
    return set()


def default_field_for_section(section_key):
    """Fallback field name when a list section edit is supplied with an item_id or invalid field."""
    canon = resolve_section_key(section_key)
    fields = _section_fields(canon)
    priority_order = ("metric", "background", "description", "headline", "stream", "market", "round", "title", "name", "category", "type")
    for pref in priority_order:
        if pref in fields:
            return pref
    if fields:
        return sorted(list(fields))[0]
    return "description"


def _find_item_index(items, *, item_id=None, item_name=None, item_index=None, reason="", evidence=None, field=None, value=""):
    """Find index of target item in an array section using ID, name, index, or text matching."""
    if not isinstance(items, list) or not items:
        return None

    # 1. Match by item_id
    if item_id not in (None, ""):
        target_id = str(item_id).strip()
        for i, item in enumerate(items):
            if isinstance(item, dict) and str(item.get("id", "")).strip() == target_id:
                return i

    # 2. Match by item_name against identifying fields
    if item_name not in (None, ""):
        target_name = _norm_str(item_name)
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for id_key in ("name", "title", "market", "stream", "metric", "round", "role", "headline"):
                val = _norm_str(item.get(id_key))
                if val and (val == target_name or target_name in val or val in target_name):
                    return i

    # 3. Match by explicit numeric item_index
    if item_index is not None:
        try:
            idx = int(item_index)
            if 0 <= idx < len(items):
                return idx
        except (ValueError, TypeError):
            pass

    # 4. Search reason/evidence/value text for item names/titles
    combined_text = _norm_str(f"{reason} {value} {' '.join(_evidence(evidence))}")
    if combined_text:
        for i, item in enumerate(items):
            if not isinstance(item, dict):
                continue
            for id_key in ("name", "title", "market", "stream", "metric", "round", "headline"):
                val = _norm_str(item.get(id_key))
                if val and len(val) > 2 and val in combined_text:
                    return i

    # 5. Fallback: if only 1 item in list, return index 0
    if len(items) == 1:
        return 0

    # 6. Fallback: first item with target field present
    if field:
        for i, item in enumerate(items):
            if isinstance(item, dict) and item.get(field):
                return i

    return 0 if items else None


def _item_summary(item):
    """Human-readable string representation of a list item dict."""
    if not isinstance(item, dict):
        return str(item or "")
    parts = []
    for k in ("metric", "name", "title", "role", "market", "stream", "value", "description", "background", "headline"):
        v = str(item.get(k) or "").strip()
        if v:
            parts.append(v)
            if len(parts) >= 2:
                break
    return " — ".join(parts) if parts else str(item.get("id") or "Item")


def _current_value(profile, section_key, field, *, item_id=None, item_name=None, item_index=None, reason="", evidence=None, action="edit"):
    """What the profile says today for scalar fields or list section item fields."""
    from fundos.profile.spec_serializer import serialize_section

    canon_key = resolve_section_key(section_key)
    try:
        data = serialize_section(profile, canon_key).get("data")
    except Exception as exc:
        logger.debug("PROPOSAL: %s unreadable: %s", canon_key, exc)
        return "" if action in ("add", "edit") else None

    if action == "add":
        return ""

    if isinstance(data, dict):
        val = data.get(field)
        if isinstance(val, str):
            return val
        if isinstance(val, (int, float, bool)):
            return str(val)
        if val is None:
            for sub_k, sub_v in data.items():
                if isinstance(sub_v, list) and sub_v:
                    idx = _find_item_index(sub_v, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, field=field)
                    if idx is not None and isinstance(sub_v[idx], dict):
                        if action == "delete":
                            return _item_summary(sub_v[idx])
                        item_val = sub_v[idx].get(field)
                        if isinstance(item_val, (str, int, float, bool)):
                            return str(item_val)
                        return _item_summary(sub_v[idx])
            return "" if field in data else ""
        if isinstance(val, list):
            idx = _find_item_index(val, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, field=field)
            if idx is not None and 0 <= idx < len(val):
                item_val = val[idx]
                if action == "delete":
                    return _item_summary(item_val)
                if isinstance(item_val, dict):
                    return item_val.get(field, item_val.get("description", item_val.get("name", _item_summary(item_val))))
                elif isinstance(item_val, str):
                    return item_val
            return ""
        return ""

    elif isinstance(data, list):
        idx = _find_item_index(data, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, field=field)
        if idx is not None and 0 <= idx < len(data) and isinstance(data[idx], dict):
            if action == "delete":
                return _item_summary(data[idx])
            val = data[idx].get(field)
            if val is None:
                for k, v in data[idx].items():
                    if k != "id" and v is not None and str(v).strip():
                        val = v
                        break
            return str(val) if val is not None else ""
        return ""

    return "" if action in ("add", "edit") else None


def validate(profile, raw, *, allowed=None):
    """Turn whatever the model returned into proposals that can be applied.

    Anything that cannot be applied is dropped and logged rather than shown.
    Supports action: "edit", "add", "delete".
    """
    allowed = editable_fields() if allowed is None else allowed
    out = []
    if not isinstance(raw, list):
        return out

    def _norm(s):
        s = re.sub(r'([a-z0-9])([A-Z])', r'\1_\2', str(s or "").strip()).lower()
        return re.sub(r'[\s\-]+', '_', s)

    norm_allowed = {(_norm(s_key), _norm(f_key)): (s_key, f_key) for s_key, f_key in allowed}

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        raw_s_key = str(item.get("sectionKey") or item.get("section_key") or item.get("section") or "").strip()
        raw_field = str(item.get("field") or item.get("fieldKey") or item.get("field_key") or "").strip()
        if raw_field and "__" in raw_field:
            raw_field = raw_field.split("__", 1)[1]
        if raw_field in FIELD_ALIASES:
            raw_field = FIELD_ALIASES[raw_field]
        action = str(item.get("action") or "edit").strip().lower()
        if action not in ("edit", "add", "delete"):
            action = "edit"

        value = item.get("proposedValue", item.get("proposed_value", item.get("value")))
        item_id = item.get("itemId") or item.get("item_id") or item.get("id")
        item_name = item.get("itemName") or item.get("item_name") or item.get("name")
        item_index = item.get("itemIndex") or item.get("item_index") or item.get("index")
        item_data = item.get("itemData") or item.get("item_data") or item.get("item")
        reason = str(item.get("reason") or "")[:500]
        evidence = item.get("evidence")

        canon_s_key = resolve_section_key(raw_s_key)

        if canon_s_key == "company_profile":
            reason_lower = reason.lower()
            if "post-money" in reason_lower or "post money" in reason_lower:
                raw_field = "latest_post_money_usd_mn"
            elif "pre-money" in reason_lower or "pre money" in reason_lower:
                raw_field = "latest_pre_money_usd_mn"
            elif ("total funding" in reason_lower or "funding raised" in reason_lower) and not any(k in reason_lower for k in ("post-money", "post money", "pre-money", "pre money")):
                raw_field = "total_funding_raised_usd_mn"

        norm_pair = (_norm(canon_s_key), _norm(raw_field))
        is_list = is_list_section(canon_s_key)

        if norm_pair in norm_allowed:
            section_key, field = norm_allowed[norm_pair]
        elif (canon_s_key, raw_field) in allowed:
            section_key, field = canon_s_key, raw_field
        elif is_list:
            section_key = canon_s_key
            allowed_fields = _section_fields(section_key)
            if raw_field in allowed_fields:
                field = raw_field
            else:
                field = default_field_for_section(section_key)
        elif action in ("add", "delete") and canon_s_key:
            section_key = canon_s_key
            field = raw_field if raw_field else "name"
        else:
            logger.info("PROPOSAL: dropped %r on %r — not a field the chat may change.", raw_field, raw_s_key)
            continue

        if action != "delete":
            if isinstance(value, str) and value.strip():
                value = value.strip()
            elif isinstance(item_data, dict):
                value = _item_summary(item_data)
            elif isinstance(value, (int, float, bool)):
                value = str(value)
            else:
                logger.info("PROPOSAL: dropped %r on %r — no text proposed.", field, section_key)
                continue
        else:
            value = "[Delete Item]"

        if len(value) > MAX_VALUE_CHARS:
            logger.info("PROPOSAL: dropped %r on %r — %d characters is a document, not an edit.", field, section_key, len(value))
            continue

        current = _current_value(profile, section_key, field, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, action=action)
        if current is None:
            current = ""
        if action == "edit" and current.strip() == value.strip():
            continue

        out.append({
            "id": f"prop_{index + 1}",
            "action": action,
            "sectionKey": section_key,
            "field": field,
            "itemId": item_id,
            "itemName": item_name,
            "itemIndex": item_index,
            "itemData": item_data,
            "currentValue": current if isinstance(current, str) else "",
            "proposedValue": value,
            "reason": reason,
            "evidence": _evidence(evidence),
        })

    deduped = []
    seen = set()
    for p in out:
        key = (p["sectionKey"], p["field"], p["itemId"], p["itemName"], p["itemIndex"], p["action"], p["proposedValue"])
        if key not in seen:
            seen.add(key)
            deduped.append(p)

    for idx, p in enumerate(deduped):
        p["id"] = f"prop_{idx + 1}"

    return deduped


class ProposalError(Exception):
    """The apply could not be performed, with a reason for the caller."""


def apply(profile, section_key, field, value, *, user=None, question="", item_id=None, item_name=None, item_index=None, action="edit", item_data=None):
    """Write one field, add a list item, or delete a list item."""
    from fundos.profile.models import ProfileSection
    from fundos.profile.section_writer import update_section_from_data
    from fundos.profile.spec_serializer import (serialize_section, storage_key_for)

    action = (action or "edit").strip().lower()
    if action not in ("edit", "add", "delete"):
        action = "edit"

    canon_s_key = resolve_section_key(section_key)

    if value is not None and not isinstance(value, str):
        value = str(value)

    if item_id and str(item_id).startswith("prop_"):
        item_id = None

    if field and "__" in str(field):
        field = str(field).split("__", 1)[1]

    if field in FIELD_ALIASES:
        field = FIELD_ALIASES[field]

    is_list = is_list_section(canon_s_key)
    if is_list:
        section_key = canon_s_key
        allowed_fields = _section_fields(section_key)
        if field and ("-" in str(field) or len(str(field)) > 20):
            if not item_id:
                item_id = str(field)
        if field not in allowed_fields:
            field = default_field_for_section(section_key)
    else:
        allowed = editable_fields()
        if (canon_s_key, field) in allowed:
            section_key = canon_s_key
        else:
            section_key = canon_s_key

    val_str = str(value).strip() if value is not None else ""
    if action != "delete" and not val_str and not isinstance(item_data, dict):
        raise ProposalError("A proposed value or item payload must be provided.")
    if len(val_str) > MAX_VALUE_CHARS:
        raise ProposalError(f"A value of {len(val_str)} characters is a document, not an edit.")
    value = val_str

    data = serialize_section(profile, section_key).get("data")
    previous = ""
    idx = None

    if isinstance(data, dict):
        if action == "edit":
            if field in data and isinstance(data[field], str):
                previous = data.get(field) or ""
                data = dict(data)
                data[field] = value.strip()
            else:
                updated = False
                data = dict(data)
                for sub_k, sub_v in data.items():
                    if isinstance(sub_v, list):
                        idx = _find_item_index(sub_v, item_id=item_id, item_name=item_name, item_index=item_index, reason=question, field=field, value=value)
                        if idx is not None and isinstance(sub_v[idx], dict):
                            sub_v = list(sub_v)
                            target = dict(sub_v[idx])
                            previous = target.get(field) or ""
                            target[field] = value.strip()
                            sub_v[idx] = target
                            data[sub_k] = sub_v
                            updated = True
                            break
                if not updated:
                    previous = data.get(field) or ""
                    data[field] = value.strip()

        elif action == "add":
            data = dict(data)
            new_obj = dict(item_data) if isinstance(item_data, dict) else {field: value.strip()}
            if item_name and "name" not in new_obj:
                new_obj["name"] = str(item_name)
            for sub_k, sub_v in data.items():
                if isinstance(sub_v, list):
                    sub_v = list(sub_v)
                    sub_v.append(new_obj)
                    data[sub_k] = sub_v
                    break

        elif action == "delete":
            data = dict(data)
            for sub_k, sub_v in data.items():
                if isinstance(sub_v, list):
                    idx = _find_item_index(sub_v, item_id=item_id, item_name=item_name, item_index=item_index, reason=question, field=field, value=value)
                    if idx is not None and 0 <= idx < len(sub_v):
                        sub_v = list(sub_v)
                        previous = _item_summary(sub_v[idx])
                        sub_v.pop(idx)
                        data[sub_k] = sub_v
                        break

    elif isinstance(data, list):
        data = list(data)
        idx = _find_item_index(data, item_id=item_id, item_name=item_name, item_index=item_index, reason=question, field=field, value=value)

        if action == "edit":
            if idx is not None and 0 <= idx < len(data) and isinstance(data[idx], dict):
                target = dict(data[idx])
                previous = target.get(field) or ""
                target[field] = value.strip()
                data[idx] = target
            else:
                new_item = {field: value.strip()}
                if item_id:
                    new_item["id"] = str(item_id)
                if item_name and field != "name":
                    new_item["name"] = str(item_name)
                data.append(new_item)

        elif action == "add":
            new_item = dict(item_data) if isinstance(item_data, dict) else {}
            if not new_item.get("id"):
                new_item["id"] = str(uuid.uuid4())
            if not new_item.get(field) and value.strip():
                new_item[field] = value.strip()
            if item_name and "name" not in new_item:
                new_item["name"] = str(item_name)
            data.append(new_item)

        elif action == "delete":
            if idx is not None and 0 <= idx < len(data):
                previous = _item_summary(data[idx])
                data.pop(idx)

    update_section_from_data(
        profile, section_key, data, user=user,
        change_summary=f"Applied chat proposal ({action}) for {field} on {section_key}")

    internal_key = storage_key_for(section_key)
    section = ProfileSection.objects.filter(
        profile=profile, section_key=internal_key, is_active=True).first()
    if section is not None:
        if action == "delete":
            _unconfirm(section, field, item_id=item_id, item_index=idx, data=data)
        else:
            fresh_data = serialize_section(profile, section_key).get("data")
            _confirm_proposal_field(section, field, item_id=item_id, item_name=item_name, item_index=idx, data=fresh_data)
        _record_provenance(section, field, question, user, item_id=item_id)

    logger.info("PROPOSAL: applied %s proposal (%r) on %r for profile %s.", action, field, section_key, profile.id)
    return {
        "action": action,
        "sectionKey": section_key,
        "field": field,
        "itemId": item_id,
        "itemName": item_name,
        "previousValue": previous if isinstance(previous, str) else "",
        "value": (value.strip() if isinstance(value, str) else ""),
        "data": serialize_section(profile, section_key).get("data")
    }


def apply_batch(profile, proposals_list, *, user=None, question=""):
    """Apply a list of proposals in a single batch transaction."""
    if not isinstance(proposals_list, list) or not proposals_list:
        raise ProposalError("A list of proposals to apply is required.")

    applied_results = []
    for prop in proposals_list:
        if not isinstance(prop, dict):
            continue
        s_key = prop.get("sectionKey") or prop.get("section_key") or prop.get("section") or ""
        field = prop.get("field") or prop.get("fieldKey") or prop.get("field_key") or ""
        field_id = prop.get("field_id") or prop.get("fieldId") or ""
        if field_id and "__" in str(field_id):
            parts = str(field_id).split("__", 1)
            if not s_key:
                s_key = parts[0]
            if not field:
                field = parts[1]
        action = prop.get("action", "edit")
        item_id = prop.get("itemId") or prop.get("item_id")
        item_name = prop.get("itemName") or prop.get("item_name")
        item_index = prop.get("itemIndex") or prop.get("item_index")
        item_data = prop.get("itemData") or prop.get("item_data") or prop.get("item")

        val = prop.get("value") if prop.get("value") is not None else prop.get("proposedValue", prop.get("proposed_value", ""))

        res = apply(profile, s_key, field, val, user=user, question=question,
                    item_id=item_id, item_name=item_name, item_index=item_index,
                    action=action, item_data=item_data)
        applied_results.append(res)
    return applied_results


def _confirm_proposal_field(section, field, item_id=None, item_name=None, item_index=None, data=None):
    """When a proposal is applied by the user, mark that field or item as confirmed globally."""
    confirmed = list(section.confirmed_fields or [])
    sec_key = section.section_key
    from fundos.profile.schema import wire_key_for, storage_key_for
    w_key = wire_key_for(sec_key)
    s_key = storage_key_for(sec_key)

    targets_to_add = set()

    if isinstance(data, list):
        targets_to_add.add(sec_key)
        targets_to_add.add(w_key)
        targets_to_add.add(s_key)
        targets_to_add.add(f"{sec_key}__array")
        targets_to_add.add("array")
        for idx, item in enumerate(data):
            if isinstance(item, dict) and item.get("id"):
                targets_to_add.add(str(item["id"]))
            targets_to_add.add(str(idx))
            targets_to_add.add(str(idx + 1))
    else:
        if field:
            targets_to_add.add(str(field))
            if field in FIELD_ALIASES:
                targets_to_add.add(FIELD_ALIASES[field])
            for k_alias, k_canon in FIELD_ALIASES.items():
                if field == k_canon:
                    targets_to_add.add(k_alias)
        if item_id:
            targets_to_add.add(str(item_id))

    new_confirmed = list(confirmed)
    for t in targets_to_add:
        if t not in new_confirmed:
            new_confirmed.append(t)

    from fundos.profile.spec_serializer import normalize_confirmed_fields
    normalized = normalize_confirmed_fields(sec_key, data, new_confirmed)
    section.confirmed_fields = normalized
    section.save(update_fields=["confirmed_fields", "updated_at"])
    logger.info("PROPOSAL: Applied proposal confirmed %r for section %r.", targets_to_add, sec_key)


def _unconfirm(section, field, item_id=None, item_index=None, data=None, previous_data=None):
    """A confirmed field or item that CHANGES is no longer confirmed."""
    confirmed = list(section.confirmed_fields or [])
    if not confirmed:
        return

    sec_key = section.section_key
    remove_targets = {str(field)}
    if item_id:
        remove_targets.add(str(item_id))

    new_confirmed = [c for c in confirmed if c not in remove_targets]
    section.confirmed_fields = new_confirmed
    section.save(update_fields=["confirmed_fields", "updated_at"])


def _record_provenance(section, field, question, user, item_id=None):
    """Record provenance when proposal is applied."""
    meta = dict(getattr(section, "field_sources", None) or {})
    key = f"{field}:{item_id}" if item_id else field
    meta[key] = {
        "source": "chat_proposal",
        "question": question[:200] if question else "",
        "applied_by": getattr(user, "username", "founder"),
    }
    section.field_sources = meta
    section.save(update_fields=["field_sources", "updated_at"])

