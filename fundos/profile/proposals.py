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
Array items (founders, competitors, products, etc.) are matched by itemId,
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
}


def editable_fields():
    """``{(section_key, field)}`` the chat may propose a change to, across object and list sections."""
    from fundos.profile.schema import sections

    out = set()
    for section in sections():
        key = section["key"]
        for field, spec in (section.get("fields") or {}).items():
            if (key, field) in CONTROLLED_FIELDS:
                continue
            out.add((key, field))

    for alias_k, canonical_k in FIELD_ALIASES.items():
        out.add(("company_profile", alias_k))

    return out


def _norm_str(s):
    return re.sub(r'[\s\-_]+', ' ', str(s or '').strip()).lower()


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


def default_field_for_section(section_key):
    """Fallback field name when a list section edit is supplied with an item_id or invalid field."""
    defaults = {
        "founders": "background",
        "products_services": "description",
        "customers_markets": "description",
        "competitive_advantages": "description",
        "revenue_model": "description",
        "competitors": "description",
        "company_metrics": "metric",
        "funding_history": "round",
        "news": "headline",
        "company_profile": "description_of_business",
        "business_model": "value_proposition",
    }
    return defaults.get(str(section_key or "").strip().lower(), "background")


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
            for id_key in ("name", "title", "market", "stream", "metric", "round", "role"):
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
            for id_key in ("name", "title", "market", "stream", "metric", "round"):
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
    for k in ("name", "title", "role", "market", "stream", "metric", "description", "background"):
        v = str(item.get(k) or "").strip()
        if v:
            parts.append(v)
            if len(parts) >= 2:
                break
    return " — ".join(parts) if parts else str(item.get("id") or "Item")


def _current_value(profile, section_key, field, *, item_id=None, item_name=None, item_index=None, reason="", evidence=None, action="edit"):
    """What the profile says today for scalar fields or list section item fields."""
    from fundos.profile.spec_serializer import serialize_section

    try:
        data = serialize_section(profile, section_key).get("data")
    except Exception as exc:                # pragma: no cover - never fatal
        logger.debug("PROPOSAL: %s unreadable: %s", section_key, exc)
        return None

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
            return "" if field in data else None
        if isinstance(val, list):
            idx = _find_item_index(val, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, field=field)
            if idx is not None and 0 <= idx < len(val):
                item_val = val[idx]
                if action == "delete":
                    return _item_summary(item_val)
                if isinstance(item_val, dict):
                    return item_val.get(field, item_val.get("description", item_val.get("name", "")))
                elif isinstance(item_val, str):
                    return item_val
            return ""
        return None

    elif isinstance(data, list):
        idx = _find_item_index(data, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, field=field)
        if idx is not None and 0 <= idx < len(data) and isinstance(data[idx], dict):
            if action == "delete":
                return _item_summary(data[idx])
            val = data[idx].get(field)
            return val if isinstance(val, str) else ("" if val is None else None)
        return ""

    return None


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
        raw_s_key = str(item.get("sectionKey") or item.get("section_key") or "").strip()
        raw_field = str(item.get("field") or item.get("fieldKey") or "").strip()
        if raw_field and "__" in raw_field:
            raw_field = raw_field.split("__", 1)[1]
        if raw_field in FIELD_ALIASES:
            raw_field = FIELD_ALIASES[raw_field]
        action = str(item.get("action") or "edit").strip().lower()
        if action not in ("edit", "add", "delete"):
            action = "edit"

        value = item.get("proposedValue", item.get("value"))
        item_id = item.get("itemId") or item.get("item_id") or item.get("id")
        item_name = item.get("itemName") or item.get("item_name") or item.get("name")
        item_index = item.get("itemIndex") or item.get("item_index") or item.get("index")
        item_data = item.get("itemData") or item.get("item")
        reason = str(item.get("reason") or "")[:500]
        evidence = item.get("evidence")

        if raw_s_key in ("company_profile", "company_overview"):
            reason_lower = reason.lower()
            if "post-money" in reason_lower or "post money" in reason_lower:
                raw_field = "latest_post_money_usd_mn"
            elif "pre-money" in reason_lower or "pre money" in reason_lower:
                raw_field = "latest_pre_money_usd_mn"
            elif ("total funding" in reason_lower or "funding raised" in reason_lower) and not any(k in reason_lower for k in ("post-money", "post money", "pre-money", "pre money")):
                raw_field = "total_funding_raised_usd_mn"

        norm_pair = (_norm(raw_s_key), _norm(raw_field))
        if norm_pair in norm_allowed:
            section_key, field = norm_allowed[norm_pair]
        elif (raw_s_key, raw_field) in allowed:
            section_key, field = raw_s_key, raw_field
        elif action in ("add", "delete") and raw_s_key:
            section_key = raw_s_key
            field = raw_field if (raw_s_key, raw_field) in allowed else "name"
        else:
            logger.info("PROPOSAL: dropped %r on %r — not a field the chat may change.", raw_field, raw_s_key)
            continue

        val_str = str(value).strip() if value is not None else ""
        if action == "delete":
            value = "[Delete Item]"
        elif not val_str:
            if isinstance(item_data, dict):
                value = _item_summary(item_data)
            else:
                logger.info("PROPOSAL: dropped %r on %r — no text proposed.", field, section_key)
                continue
        else:
            value = val_str

        if len(value) > MAX_VALUE_CHARS:
            logger.info("PROPOSAL: dropped %r on %r — %d characters is a document, not an edit.", field, section_key, len(value))
            continue

        current = _current_value(profile, section_key, field, item_id=item_id, item_name=item_name, item_index=item_index, reason=reason, evidence=evidence, action=action)
        if current is None:
            continue
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
            "itemData": item_data if isinstance(item_data, dict) else None,
            "currentValue": current,
            "proposedValue": value.strip(),
            "reason": reason,
            "evidence": _evidence(evidence),
        })

    # Deduplicate proposals targeting the same (sectionKey, field, itemId)
    deduped = []
    seen = {}
    for p in out:
        key = (p["sectionKey"], p["field"], str(p.get("itemId") or ""))
        if key not in seen:
            seen[key] = len(deduped)
            deduped.append(p)
        else:
            existing_idx = seen[key]
            existing_val = str(deduped[existing_idx].get("proposedValue", ""))
            new_val = str(p.get("proposedValue", ""))
            # Prefer plain numeric string (e.g. "12.0") over formatted display string ("USD:12:M")
            if "usd:" in existing_val.lower() and "usd:" not in new_val.lower():
                deduped[existing_idx] = p

    # Re-assign clean IDs
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

    if item_id and str(item_id).startswith("prop_"):
        item_id = None

    if field and "__" in str(field):
        field = str(field).split("__", 1)[1]

    if field in FIELD_ALIASES:
        field = FIELD_ALIASES[field]

    allowed = editable_fields()
    if (section_key, field) not in allowed:
        if section_key in ("founders", "products_services", "customers_markets", "competitive_advantages", "revenue_model", "company_metrics", "funding_history", "competitors", "news"):
            if field and ("-" in str(field) or len(str(field)) > 20):
                if not item_id:
                    item_id = str(field)
            field = default_field_for_section(section_key)

    val_str = str(value).strip() if value is not None else ""
    if action != "delete" and not val_str and not isinstance(item_data, dict):
        raise ProposalError("A proposed value or item payload must be provided.")
    if len(val_str) > MAX_VALUE_CHARS:
        raise ProposalError(f"A value of {len(val_str)} characters is a document, not an edit.")
    value = val_str

    data = serialize_section(profile, section_key).get("data")
    previous = ""

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
            # Find sub list if any
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
            new_item = dict(item_data) if isinstance(item_data, dict) else {field: value.strip()}
            if item_id and "id" not in new_item:
                new_item["id"] = str(item_id)
            if item_name and "name" not in new_item and field != "name":
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
        _unconfirm(section, field)
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
        s_key = prop.get("sectionKey") or prop.get("section_key") or ""
        field = prop.get("field") or prop.get("fieldKey") or ""
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
        item_data = prop.get("itemData") or prop.get("item")

        if field and "__" in str(field):
            field = str(field).split("__", 1)[1]
        if field in FIELD_ALIASES:
            field = FIELD_ALIASES[field]

        if (s_key, field) not in editable_fields():
            if s_key in ("founders", "products_services", "customers_markets", "competitive_advantages", "revenue_model", "company_metrics", "funding_history", "competitors", "news"):
                if field and ("-" in str(field) or len(str(field)) > 20):
                    if not item_id:
                        item_id = str(field)
                field = default_field_for_section(s_key)

        val = prop.get("value") if prop.get("value") is not None else prop.get("proposedValue", prop.get("proposed_value", ""))

        res = apply(profile, s_key, field, val, user=user, question=question,
                    item_id=item_id, item_name=item_name, item_index=item_index,
                    action=action, item_data=item_data)
        applied_results.append(res)
    return applied_results


def _unconfirm(section, field):
    """A confirmed field that CHANGES is no longer confirmed."""
    confirmed = list(section.confirmed_fields or [])
    if field not in confirmed:
        return
    section.confirmed_fields = [f for f in confirmed if f != field]
    section.save(update_fields=["confirmed_fields", "updated_at"])
    logger.info("PROPOSAL: %r was confirmed and has changed — the confirmation is cleared, not carried over.", field)


def _record_provenance(section, field, question, user, item_id=None):
    """Say where this value came from while preserving original dossier sources."""
    sources = dict(section.field_sources or {})
    target_keys = set()
    if field:
        target_keys.add(str(field))
    if item_id:
        target_keys.add(str(item_id))

    chat_source = {
        "source": "Profile chat",
        "locator": (str(question)[:200] if question else "Applied chat proposal"),
        "quote": "",
        "appliedBy": getattr(user, "email", "") or "",
    }

    for target_key in target_keys:
        existing = sources.get(target_key)
        if isinstance(existing, dict) and existing.get("source") and existing.get("source") != "Profile chat":
            if not sources.get(f"{target_key}.orig"):
                sources[f"{target_key}.orig"] = existing

        sources[target_key] = chat_source

    section.field_sources = sources
    section.save(update_fields=["field_sources", "updated_at"])
