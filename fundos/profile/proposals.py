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

TEXT FIELDS ONLY, deliberately. Money carries a currency and a scale, and a
bare number proposed for one is the 83x misread this codebase has already
had to correct. A list row is addressed by its own id, and "the third
competitor" stops being the same row the moment one is inserted. The chat
can still discuss both; it cannot propose an applyable change to them until
those two carry their own handling.
"""
import logging

logger = logging.getLogger(__name__)

#: A field spec the model is given describes the type in its first word.
#: "string - primary website URL" is one of ours; "array of strings" and
#: "number|null" are not.
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


def editable_fields():
    """``{(section_key, field)}`` the chat may propose a change to."""
    from fundos.profile.schema import SHIPPED_SECTIONS

    out = set()
    for section in SHIPPED_SECTIONS:
        if section.get("kind") != "object":
            continue
        key = section["key"]
        for field, spec in (section.get("fields") or {}).items():
            if not str(spec or "").strip().lower().startswith(
                    _TEXT_SPEC_PREFIX):
                continue
            if (key, field) in CONTROLLED_FIELDS:
                continue
            out.add((key, field))
    return out


def _current_value(profile, section_key, field):
    """What the profile says today. "" when the field is empty."""
    from fundos.profile.spec_serializer import serialize_section

    try:
        data = serialize_section(profile, section_key).get("data")
    except Exception as exc:                # pragma: no cover - never fatal
        logger.debug("PROPOSAL: %s unreadable: %s", section_key, exc)
        return None
    if not isinstance(data, dict):
        return None
    value = data.get(field)
    return value if isinstance(value, str) else ("" if value is None else None)


def validate(profile, raw, *, allowed=None):
    """Turn whatever the model returned into proposals that can be applied.

    Anything that cannot be applied is dropped and logged rather than shown:
    a card a founder clicks Apply on and nothing happens is worse than no
    card.
    """
    allowed = editable_fields() if allowed is None else allowed
    out = []
    if not isinstance(raw, list):
        return out

    for index, item in enumerate(raw):
        if not isinstance(item, dict):
            continue
        section_key = str(item.get("sectionKey")
                          or item.get("section_key") or "").strip()
        field = str(item.get("field") or item.get("fieldKey") or "").strip()
        value = item.get("proposedValue", item.get("value"))

        if (section_key, field) not in allowed:
            logger.info("PROPOSAL: dropped %r on %r — not a field the chat "
                        "may change.", field, section_key)
            continue
        if not isinstance(value, str) or not value.strip():
            logger.info("PROPOSAL: dropped %r on %r — no text proposed.",
                        field, section_key)
            continue
        if len(value) > MAX_VALUE_CHARS:
            logger.info("PROPOSAL: dropped %r on %r — %d characters is a "
                        "document, not an edit.", field, section_key,
                        len(value))
            continue

        current = _current_value(profile, section_key, field)
        if current is None:
            continue
        if current.strip() == value.strip():
            # Proposing what is already there gives a founder a button that
            # changes nothing.
            continue

        out.append({
            "id": f"prop_{index + 1}",
            "sectionKey": section_key,
            "field": field,
            "currentValue": current,
            "proposedValue": value.strip(),
            "reason": str(item.get("reason") or "")[:500],
            "evidence": _evidence(item.get("evidence")),
        })
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


class ProposalError(Exception):
    """The apply could not be performed, with a reason for the caller."""


def apply(profile, section_key, field, value, *, user=None, question=""):
    """Write one field, the same way the form writes it.

    READ, MODIFY, WRITE, in one place. A section PATCH replaces the whole
    section, so a client assembling the body from a proposal would have to
    send back every other field too -- and anything edited in another tab
    meanwhile would be clobbered by a payload built before that edit existed.

    Returns the updated section payload.
    """
    from fundos.profile.models import ProfileSection
    from fundos.profile.section_writer import update_section_from_data
    from fundos.profile.spec_serializer import (serialize_section,
                                                storage_key_for)

    if (section_key, field) not in editable_fields():
        raise ProposalError(
            f"{field!r} on {section_key!r} is not a field the chat may "
            f"change.")
    if not isinstance(value, str) or not value.strip():
        raise ProposalError("A proposed value must be text.")
    if len(value) > MAX_VALUE_CHARS:
        raise ProposalError(
            f"A value of {len(value)} characters is a document, not an edit.")

    data = serialize_section(profile, section_key).get("data")
    if not isinstance(data, dict):
        raise ProposalError(f"{section_key!r} does not hold editable fields.")
    if field not in data:
        raise ProposalError(f"{field!r} is not part of {section_key!r}.")

    previous = data.get(field)
    data = dict(data)
    data[field] = value.strip()

    update_section_from_data(
        profile, section_key, data, user=user,
        change_summary=f"Applied a chat proposal for {field}")

    internal_key = storage_key_for(section_key)
    section = ProfileSection.objects.filter(
        profile=profile, section_key=internal_key, is_active=True).first()
    if section is not None:
        _unconfirm(section, field)
        _record_provenance(section, field, question, user)

    logger.info("PROPOSAL: applied %r on %r for profile %s.", field,
                section_key, profile.id)
    return {"sectionKey": section_key, "field": field,
            "previousValue": previous if isinstance(previous, str) else "",
            "value": value.strip(),
            "data": serialize_section(profile, section_key).get("data")}


def _unconfirm(section, field):
    """A confirmed field that CHANGES is no longer confirmed.

    The founder confirmed the text they read. Leaving the confirmation in
    place would have the readiness score crediting a confirmation for words
    they have never seen.
    """
    confirmed = list(section.confirmed_fields or [])
    if field not in confirmed:
        return
    section.confirmed_fields = [f for f in confirmed if f != field]
    section.save(update_fields=["confirmed_fields", "updated_at"])
    logger.info("PROPOSAL: %r was confirmed and has changed — the "
                "confirmation is cleared, not carried over.", field)


def _record_provenance(section, field, question, user):
    """Say where this value came from, so the trail does not read as research.

    Six weeks later nobody remembers that a sentence was rewritten in a chat.
    Without this the field carries whatever source synthesis recorded for the
    text it replaced, which is a citation for words that are no longer there.
    """
    sources = dict(section.field_sources or {})
    sources[field] = {
        "source": "Profile chat",
        "locator": (str(question)[:200] if question else ""),
        "quote": "",
        "appliedBy": getattr(user, "email", "") or "",
    }
    section.field_sources = sources
    section.save(update_fields=["field_sources", "updated_at"])
