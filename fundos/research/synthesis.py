"""
Research synthesis (Doc 1 M1-§1 steps 1.2–1.3).

LLM-M1-002: the research_synthesis role summarises multi-source payloads
into structured CKB fields with fact/inference separation (LLM-M1-003).
BR-M1-002: research PROPOSES CKB values — never overwrites verified fields
(ckb_service enforces BR-M0-011).
BR-M1-004: inconsistencies across sources are surfaced, never silently
resolved.
"""
import logging

from django.utils import timezone

logger = logging.getLogger(__name__)


def synthesise(deal, sources_payloads: dict, *, user=None) -> dict:
    """sources_payloads: {source_type: payload}. Returns
    {"proposed": [...], "inconsistencies": [...]}."""
    from fundos.core.services import ckb_service
    from fundos.llm import context as llm_context
    from fundos.llm.adapter import llm_generate

    ctx = llm_context.build("research_synthesis", deal)
    ctx["research_payloads"] = sources_payloads

    data = llm_generate(
        role="research_synthesis",
        system=("You are an investment-research analyst. Synthesise the "
                "multi-source research payloads into structured company "
                "fields. For each field return fieldKey, value, "
                "factOrInference ('fact' when directly observed in a source, "
                "'inference' otherwise), evidence (list of source types) and "
                "confidence (0-1). Return JSON: {\"fields\": [...]}. Do not "
                "invent numbers absent from the payloads."),
        prompt="Synthesise the research into CKB fields.",
        context=ctx, deal_id=deal.id, user=user,
        calling_context="research.synthesis",
    )

    proposed = []
    seen_values = {}
    inconsistencies = []
    for item in data.get("fields", []):
        field_key = item.get("fieldKey")
        if not field_key:
            continue
        value = item.get("value")
        # BR-M1-004 — detect cross-source contradictions per field.
        if field_key in seen_values and seen_values[field_key] != value:
            inconsistencies.append({
                "fieldKey": field_key,
                "values": [seen_values[field_key], value],
                "message": f"Sources disagree on {field_key}; review required.",
            })
            continue
        seen_values[field_key] = value

        field, _ = ckb_service.set_field(
            deal, field_key, value, user=user, source="ai_research",
            confidence=item.get("confidence"),
            fact_or_inference=item.get("factOrInference", ""),
            source_ref=",".join(item.get("evidence", []))[:255],
            verify=False,
        )
        proposed.append({
            "fieldKey": field_key, "value": value,
            "factOrInference": item.get("factOrInference"),
            "evidence": item.get("evidence", []),
            "confidence": item.get("confidence"),
            "parkedAsSuggestion": bool(field.verified),  # BR-M0-011 path
        })
    return {"proposed": proposed, "inconsistencies": inconsistencies}


def peer_stats_from_sources(deal_id) -> dict:
    """Placeholder peer statistics until the market feed is ratified
    (Doc 7 critical path) — engines run on seeded fixtures."""
    return {}
