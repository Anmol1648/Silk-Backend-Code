"""
CKB service (Doc 1 M0-§2 / M1-§2).

BR-M0-010 provenance · BR-M0-011 AI never overwrites verified ·
BR-M0-012 edit-once-propagate-everywhere · BR-M0-013 versioned history ·
VAL-M0-010 numeric validation · LLM-M1-011 soft consistency warnings.
"""
import logging
from decimal import Decimal, InvalidOperation

from django.db import transaction
from django.utils import timezone

from fundos.core.exceptions import DomainValidationError
from fundos.core.services import recalc
from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)

# VAL-M0-010 — numeric fields validated to type/range.
NUMERIC_RULES = {
    "arr": {"min": 0}, "mrr": {"min": 0}, "revenue": {"min": 0},
    "ebitda": {},  # may be negative
    "burn": {"min": 0}, "runway_months": {"min": 0, "max": 240},
    "gross_margin": {"min": -100, "max": 100},
    "churn": {"min": 0, "max": 100},
    "cac": {"min": 0}, "ltv": {"min": 0},
    "customers": {"min": 0}, "paying_customers": {"min": 0},
    "employees": {"min": 0}, "founding_year": {"min": 1900, "max": 2100},
    "growth_rate": {"min": -100, "max": 10000},
}

FIELD_GROUPS = {
    "description": "company", "vision": "company", "mission": "company",
    "problem": "company", "solution": "company", "products_services": "company",
    "sector": "business", "subsector": "business", "business_model": "business",
    "revenue_model": "business", "pricing": "business", "segments": "business",
    "geography": "business", "gtm": "business",
    "revenue": "financial", "arr": "financial", "mrr": "financial",
    "ebitda": "financial", "burn": "financial", "runway_months": "financial",
    "gross_margin": "financial", "cac": "financial", "ltv": "financial",
    "churn": "financial", "growth_rate": "financial", "cash_balance": "financial",
    "customers": "customers", "paying_customers": "customers",
    "enterprise_customers": "customers", "customer_concentration": "customers",
    "major_customers": "customers",
    "org_structure": "team", "leadership": "team", "key_hires": "team",
    "esop": "team", "employees": "team", "founders": "team",
    "cap_table": "shareholding", "existing_investors": "shareholding",
    "board": "shareholding", "funding_history": "shareholding",
    "incorporation": "legal", "subsidiaries": "legal", "licences": "legal",
    "ip": "legal", "litigation": "legal", "compliance": "legal",
    "founding_year": "company",
}

MONETARY_FIELDS = {"arr", "mrr", "revenue", "ebitda", "burn", "cac", "ltv",
                   "cash_balance"}


def get_or_create_ckb(deal):
    from fundos.core.models import CompanyKnowledgeBase
    ckb, _ = CompanyKnowledgeBase.objects.get_or_create(
        deal_id=deal.id, defaults={"tenant_id": deal.tenant_id})
    return ckb


def _validate_numeric(field_key, value):
    rules = NUMERIC_RULES.get(field_key)
    if rules is None:
        return None
    try:
        num = Decimal(str(value))
    except (InvalidOperation, TypeError, ValueError):
        raise DomainValidationError(
            f"{field_key} must be numeric.", fields={field_key: "not_numeric"})
    if "min" in rules and num < rules["min"]:
        raise DomainValidationError(
            f"{field_key} must be ≥ {rules['min']}.",
            fields={field_key: f"min:{rules['min']}"})
    if "max" in rules and num > rules["max"]:
        raise DomainValidationError(
            f"{field_key} must be ≤ {rules['max']}.",
            fields={field_key: f"max:{rules['max']}"})
    return num


@transaction.atomic
def set_field(deal, field_key, value, *, user=None, source="founder",
              confidence=None, ccy="", basis="", fact_or_inference="",
              source_ref="", verify=None, reason=""):
    """Write one CKB field with provenance + history + propagation.

    BR-M0-011: when `source` is AI and the field is already verified, the AI
    value is parked as a suggestion beside the verified value.
    Founder edits set verified=true (Doc 4 M0 CKB PATCH).
    """
    from fundos.core.models import CkbField, CkbFieldHistory

    ckb = get_or_create_ckb(deal)
    group_key = FIELD_GROUPS.get(field_key, "company")
    num = _validate_numeric(field_key, value) if not isinstance(value, (dict, list)) \
        and field_key in NUMERIC_RULES and value is not None else None

    field, created = CkbField.all_objects.get_or_create(
        ckb=ckb, field_key=field_key, is_deleted=False,
        defaults={"tenant_id": deal.tenant_id, "deal_id": deal.id,
                  "group_key": group_key},
    )
    old = {"text": field.value_text,
           "num": float(field.value_num) if field.value_num is not None else None,
           "json": field.value_json}

    ai_source = source in ("ai_research", "document_extracted")
    if ai_source and field.verified:
        # BR-M0-011 — never overwrite verified; park as suggestion.
        field.suggested_value_json = {
            "value": value, "source": source, "confidence": float(confidence or 0),
            "source_ref": source_ref, "at": timezone.now().isoformat(),
        }
        field.save(update_fields=["suggested_value_json", "updated_at"])
        return field, []

    field.value_text = None
    field.value_num = None
    field.value_json = None
    if isinstance(value, (dict, list)):
        field.value_json = value
    elif num is not None:
        field.value_num = num
    elif value is None:
        pass  # explicit reject → cleared value (Doc 4 M1 pillar-1)
    else:
        field.value_text = str(value)

    field.source = source
    field.confidence = confidence
    field.source_ref = source_ref[:255]
    field.fact_or_inference = fact_or_inference
    if field_key in MONETARY_FIELDS:
        field.ccy = (ccy or field.ccy or "INR")[:3]
        field.basis = basis or field.basis or "actual"

    verified = verify if verify is not None else (source == "founder")
    field.verified = bool(verified) and value is not None
    if field.verified:
        field.verified_by = user if getattr(user, "pk", None) else None
        field.verified_at = timezone.now()
    field.save()

    CkbFieldHistory.objects.create(
        tenant_id=deal.tenant_id, ckb_field=field, deal_id=deal.id,
        old_value_json=old,
        new_value_json={"text": field.value_text,
                        "num": float(field.value_num) if field.value_num is not None else None,
                        "json": field.value_json, "source": source},
        changed_by=user if getattr(user, "pk", None) else None,
        reason=reason[:255],
    )

    recompute_completeness(ckb)

    # BR-M0-012 — propagate: dependent artefacts pending recalc.
    recalc.propagate_change(deal.id, "ckb", reason=f"ckb.{field_key} changed")

    audit("ckb.field.set", actor=user, deal_id=deal.id, entity="ckb_field",
          entity_id=field.id, meta={"field_key": field_key, "source": source})

    warnings = consistency_warnings(deal)
    return field, warnings


def recompute_completeness(ckb):
    from fundos.config.feature_flags import mandatory_ckb_fields
    from fundos.core.models import CkbField
    mandatory = mandatory_ckb_fields()
    if not mandatory:
        return
    present = CkbField.objects.filter(
        ckb=ckb, field_key__in=mandatory,
    ).exclude(value_text__isnull=True, value_num__isnull=True,
              value_json__isnull=True).count()
    ckb.completeness_pct = Decimal(str(round(100.0 * present / len(mandatory), 2)))
    ckb.save(update_fields=["completeness_pct", "updated_at"])


def field_map(deal_id):
    from fundos.core.models import CkbField
    return {f.field_key: f for f in CkbField.objects.filter(deal_id=deal_id)}


def numeric(fields, key):
    f = fields.get(key)
    if f is not None and f.value_num is not None:
        return float(f.value_num)
    return None


def consistency_warnings(deal):
    """LLM-M1-011 deterministic part — advisory, never blocking.
    (The AI cross-check against research runs in the research synthesis.)"""
    fields = field_map(deal.id)
    warnings = []
    arr, mrr = numeric(fields, "arr"), numeric(fields, "mrr")
    if arr is not None and mrr is not None and mrr > 0:
        if abs(arr - mrr * 12) / max(arr, 1) > 0.10:
            warnings.append({"rule": "arr_vs_mrr",
                             "message": "ARR ≠ MRR×12 (more than 10% apart).",
                             "severity": "soft"})
    burn, runway = numeric(fields, "burn"), numeric(fields, "runway_months")
    cash = numeric(fields, "cash_balance")
    if burn and runway and cash:
        implied = cash / burn
        if abs(implied - runway) > max(2.0, 0.25 * runway):
            warnings.append({"rule": "burn_vs_runway",
                             "message": "Runway is inconsistent with cash ÷ burn.",
                             "severity": "soft"})
    return warnings


def snapshot(deal_id):
    """CKB snapshot used by Stage1.out and the AI Context Engine."""
    from fundos.core.models import CkbField
    groups = {}
    for f in CkbField.objects.filter(deal_id=deal_id):
        groups.setdefault(f.group_key, {})[f.field_key] = {
            "value": f.value, "ccy": f.ccy or None, "basis": f.basis or None,
            "source": f.source, "confidence": float(f.confidence) if f.confidence is not None else None,
            "verified": f.verified, "factOrInference": f.fact_or_inference or None,
        }
    return groups
