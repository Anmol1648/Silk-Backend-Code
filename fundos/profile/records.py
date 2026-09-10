"""
Structured profile records — CRUD for the list-style sections and the
numeric/structured forms (QA 25-Jul enhancement set).

Background
----------
Founders was already a first-class structured section: its own table, full
CRUD, and a status recompute that keeps the "needs input" dot honest. The
enhancement set asks for the *same treatment* for the other record-backed
sections — Key People, Competitors, Funding History, Recent News — and for
the numeric forms (Revenue Model, Company Metrics, Financial Summary) to
become validated structured records rather than free text.

Rather than write four near-identical view classes, the list sections are
driven by a single declarative registry (RECORD_TYPES). Each entry names
its model, its section_key (for the needs-input recompute), the field map
between API camelCase and model snake_case, and per-field coercion /
validation. One generic view then serves add / edit / delete for all of
them. Adding a fifth record section later is a registry entry, not a new
view — the same philosophy the ProfileSectionConfig already follows.

The numeric forms live in ProfileSection.structured (a JSONField that
already exists on the model), validated by STRUCTURED_FORMS below. No new
tables, so no migration for those either; the only migration in this
release backfills the section `kind` for the three forms so the frontend
renders them as forms rather than prose.
"""
import re
from decimal import Decimal, InvalidOperation

from django.utils import timezone
from rest_framework.response import Response
from rest_framework.views import APIView

from fundos.core.exceptions import DomainValidationError, NotFoundInDeal
from fundos.core.services.audit import audit


# ---------------------------------------------------------------------------
# Field coercers — shared, each raises DomainValidationError with a clear,
# client-attachable {field: message}.
# ---------------------------------------------------------------------------
def _text(v, field, *, required=False, max_len=None):
    s = "" if v is None else str(v).strip()
    if required and not s:
        raise DomainValidationError({field: "This field is required."})
    if max_len and len(s) > max_len:
        # Name the field AND show what was rejected. "Keep this under 9
        # characters." with no field and no value was the entire log line for
        # a failure that discarded every year of a company's financials —
        # unactionable without reading the source to find which field is 9.
        raise DomainValidationError(
            {field: f"{field}: keep this under {max_len} characters "
                    f"(got {len(s)}: {s[:40]!r})."})
    return s


def _url(v, field, **kw):
    s = _text(v, field, **kw)
    if s and not (s.startswith("http://") or s.startswith("https://")):
        # Be forgiving: prepend https:// rather than reject a bare domain.
        s = "https://" + s
    return s


def _decimal(v, field, *, required=False, allow_negative=False):
    if v in (None, ""):
        if required:
            raise DomainValidationError({field: "Enter a number."})
        return None
    try:
        d = Decimal(str(v))
    except (InvalidOperation, TypeError, ValueError):
        raise DomainValidationError({field: "Enter a valid number."})
    if not allow_negative and d < 0:
        raise DomainValidationError({field: "Enter a positive amount."})
    return d


def _date(v, field, **kw):
    """Accept an ISO date string or blank. Stored as-is (Django parses)."""
    s = _text(v, field, **kw)
    return s or None


def _list_of_str(v, field, **kw):
    if v in (None, ""):
        return []
    if isinstance(v, str):
        return [p.strip() for p in v.split(",") if p.strip()]
    if isinstance(v, list):
        return [str(p).strip() for p in v if str(p).strip()]
    raise DomainValidationError({field: "Provide a comma-separated list."})


def _int(v, field, *, required=False, **kw):
    if v in (None, ""):
        if required:
            raise DomainValidationError({field: "This field is required."})
        return None
    try:
        return int(v)
    except (TypeError, ValueError):
        raise DomainValidationError({field: "Enter a whole number."})


# ---------------------------------------------------------------------------
# Declarative registry for the list-style sections.
#   apiField -> (modelAttr, coercer, coercer_kwargs)
# ---------------------------------------------------------------------------
RECORD_TYPES = {
    "founders": {
        "model": "Founder",
        "section_key": "founders",
        "label_field": "name",
        "fields": {
            "name": ("name", _text, {"required": True, "max_len": 255}),
            # Spec §7.2 uses role/background; map onto the existing columns.
            "designation": ("designation", _text, {"max_len": 128}),
            "experience": ("experience", _text, {}),
            "linkedinUrl": ("linkedin_url", _url, {"max_len": 512}),
            "biography": ("biography", _text, {}),
        },
    },
    "key_people": {
        "model": "KeyPerson",
        "section_key": "key_people",
        "label_field": "name",
        "fields": {
            "name": ("name", _text, {"required": True, "max_len": 255}),
            "designation": ("designation", _text, {"max_len": 128}),
            "linkedinUrl": ("linkedin_url", _url, {"max_len": 512}),
            "biography": ("biography", _text, {}),
        },
    },
    "competitors": {
        "model": "Competitor",
        "section_key": "competitors",
        "label_field": "name",
        "fields": {
            "name": ("name", _text, {"required": True, "max_len": 255}),
            "website": ("website", _url, {"max_len": 512}),
            "description": ("description", _text, {}),
            "positioning": ("positioning", _text, {}),
            "geography": ("geography", _text, {"max_len": 128}),
            "fundingRaisedUsd": ("funding_raised_usd", _decimal, {}),
            "fyYear": ("fy_year", _int, {}),
            "revenue": ("revenue", _decimal, {}),
            "investors": ("investors", _list_of_str, {}),
        },
    },
    "funding_history": {
        "model": "FundingRound",
        "section_key": "funding_history",
        "label_field": "round_name",
        "fields": {
            "roundName": ("round_name", _text, {"max_len": 64}),
            "announcedDate": ("announced_date", _date, {}),
            "amount": ("amount_value", _decimal, {}),
            "ccy": ("amount_ccy", _text, {"max_len": 3}),
            "preMoneyValue": ("pre_money_value", _decimal, {}),
            "postMoneyValue": ("post_money_value", _decimal, {}),
            "leadInvestor": ("lead_investor", _text, {"max_len": 255}),
            "investors": ("investors", _list_of_str, {}),
            "notes": ("notes", _text, {}),
        },
    },
    "recent_news": {
        "model": "NewsItem",
        "section_key": "recent_news",
        "label_field": "headline",
        "fields": {
            "headline": ("headline", _text, {"required": True, "max_len": 512}),
            "summary": ("summary", _text, {}),
            "url": ("url", _url, {"max_len": 1024}),
            "publishedDate": ("published_date", _date, {}),
            "publisher": ("publisher", _text, {"max_len": 255}),
        },
    },
    # Cap table / investor list (requirement §2) — editable like any record.
    "cap_table": {
        "model": "Investor",
        "section_key": "cap_table",
        "label_field": "name",
        "fields": {
            "name": ("name", _text, {"required": True, "max_len": 255}),
            "investorType": ("investor_type", _text, {"max_len": 16}),
            "rounds": ("rounds", _list_of_str, {}),
            "ownershipPct": ("ownership_pct", _decimal, {}),
            "holderCategory": ("holder_category", _text, {"max_len": 16}),
        },
    },
}


def _model(name):
    from fundos.profile import models as m
    return getattr(m, name)


def _apply_fields(instance, spec, data, *, partial):
    """Coerce+assign supplied API fields onto a model instance.

    partial=True (PATCH) only touches keys present in the payload; required
    checks are skipped for absent keys. partial=False (create) applies every
    field, enforcing required.
    """
    for api_field, (attr, coerce, kw) in spec["fields"].items():
        if partial and api_field not in data:
            continue
        raw = data.get(api_field)
        kwargs = dict(kw)
        if partial:
            kwargs.pop("required", None)  # can't require what isn't being sent
        value = coerce(raw, api_field, **kwargs)
        setattr(instance, attr, value)


def _serialize(instance, spec):
    out = {"id": str(instance.id)}
    for api_field, (attr, _c, _k) in spec["fields"].items():
        val = getattr(instance, attr)
        if isinstance(val, Decimal):
            val = float(val)
        out[api_field] = val
    return out


# ---------------------------------------------------------------------------
# Generic list-record view. Routed per section_key so the URL stays
# self-describing (…/profile/records/{sectionKey}[/{recordId}]).
# ---------------------------------------------------------------------------
class ProfileRecordView(APIView):
    """Add / edit / delete one record in a list-style section.

    Mirrors FounderDetailView exactly (soft delete, recompute the section
    dot, audit), generalised across the four record types. Founders keeps
    its dedicated view because it carries confirmation semantics the others
    don't.
    """

    def _spec(self, section_key):
        spec = RECORD_TYPES.get(section_key)
        if not spec:
            raise NotFoundInDeal("Unknown profile section.")
        return spec

    def _profile(self, request, company_id):
        from fundos.profile.views import _company_or_404
        from fundos.profile.services import get_or_create_profile
        company = _company_or_404(request, company_id)
        return company, get_or_create_profile(company, user=request.user)

    def post(self, request, company_id, section_key):
        spec = self._spec(section_key)
        company, profile = self._profile(request, company_id)
        Model = _model(spec["model"])

        instance = Model(tenant_id=company.tenant_id, profile=profile,
                         source="founder", created_by=request.user)
        _apply_fields(instance, spec, request.data, partial=False)
        # place new records last
        last = (Model.objects.filter(profile=profile)
                .order_by("-sort_order").values_list("sort_order", flat=True)
                .first())
        instance.sort_order = (last or 0) + 1
        instance.save()

        _recompute(profile, section_key)
        audit("profile.record_added", actor=request.user,
              entity=spec["model"], entity_id=instance.id,
              tenant_id=company.tenant_id,
              meta={"sectionKey": section_key})
        return Response(_serialize(instance, spec), status=201)

    def patch(self, request, company_id, section_key, record_id):
        spec = self._spec(section_key)
        company, profile = self._profile(request, company_id)
        Model = _model(spec["model"])

        instance = Model.objects.filter(id=record_id, profile=profile).first()
        if not instance:
            raise NotFoundInDeal("Record not found.")
        _apply_fields(instance, spec, request.data, partial=True)
        # A human edit is a verification of that record's content.
        instance.source = instance.source or "founder"
        instance.verified = True
        instance.verified_by = request.user
        instance.verified_at = timezone.now()
        instance.save()

        _recompute(profile, section_key)
        audit("profile.record_edited", actor=request.user,
              entity=spec["model"], entity_id=instance.id,
              tenant_id=company.tenant_id,
              meta={"sectionKey": section_key})
        return Response(_serialize(instance, spec))

    def delete(self, request, company_id, section_key, record_id):
        spec = self._spec(section_key)
        company, profile = self._profile(request, company_id)
        Model = _model(spec["model"])

        instance = Model.objects.filter(id=record_id, profile=profile).first()
        if not instance:
            raise NotFoundInDeal("Record not found.")
        instance.delete(deleted_by=request.user)

        _recompute(profile, section_key)
        audit("profile.record_deleted", actor=request.user,
              entity=spec["model"], entity_id=record_id,
              tenant_id=company.tenant_id,
              meta={"sectionKey": section_key})
        return Response(status=204)


def _recompute(profile, section_key):
    """Reconcile the section's needs-input dot against its backing records."""
    from fundos.profile.services import recompute_structured_sections
    recompute_structured_sections(profile)


# ===========================================================================
# Numeric / structured forms (Revenue Model, Company Metrics, Financial
# Summary). These do not get their own tables — the shape is validated JSON
# stored on ProfileSection.structured, versioned by the existing
# update_section snapshot path.
# ===========================================================================
def _pct(v, field):
    """A share of a whole: 0–100 inclusive.

    For fields that are genuinely a proportion — a margin, a revenue split, an
    ownership stake. Growth is NOT one of these; see :func:`_growth_pct`.
    """
    d = _decimal(v, field)
    if d is None:
        return None
    if d < 0 or d > 100:
        raise DomainValidationError({field: "Enter a percentage 0–100."})
    return float(d)


def _growth_pct(v, field):
    """A rate of change, which is not bounded by 100.

    A company can grow 300% in a year, and one in this database did. Validating
    growth with :func:`_pct` would 422 the true figure and accept only an
    understated one — a validator that rejects reality in favour of a number
    that fits. Negative is allowed (revenue falls), and the bound that does
    apply is a sanity ceiling: beyond ±100,000% the value is a unit error, not
    a business.
    """
    d = _decimal(v, field, allow_negative=True)
    if d is None:
        return None
    if d < -100 or d > 100_000:
        raise DomainValidationError({
            field: f"{field}: a growth rate of {d}% is outside anything "
                   f"plausible — check whether this is a multiple rather than "
                   f"a percentage."})
    return float(d)


def _bool(v, field, *, default=False, **kw):
    """A tolerant boolean. Never raises — a checkbox is not worth a 422."""
    if isinstance(v, bool):
        return v
    if v in (None, ""):
        return default
    if isinstance(v, str):
        return v.strip().lower() in ("true", "1", "yes", "y", "t")
    return bool(v)


def _ccy(v, field, **kw):
    """An ISO-4217 code, upper-cased, or blank.

    Three letters or nothing: a currency field holding "Rs" or "INR crore"
    is worse than an empty one, because every downstream reader treats a
    populated `ccy` as authoritative and none of them parse prose.
    """
    s = _text(v, field, max_len=32).strip().upper()
    if not s:
        return ""
    if not re.fullmatch(r"[A-Z]{3}", s):
        raise DomainValidationError({
            field: f"{field}: use a three-letter ISO currency code such as "
                   f"INR or USD (got {s!r})."})
    return s


STRUCTURED_FORMS = {
    # Revenue split must sum to ~100%. Each stream is {label, pct}.
    "revenue_model": {
        "kind": "streams",
        "sum_to_100": True,
        "item": {"label": ("label", _text, {"required": True}),
                 "pct": ("pct", _pct, {})},
    },
    # Company metrics — free-form typed KPI rows {label, value, unit}.
    "company_metrics": {
        "kind": "metrics",
        "sum_to_100": False,
        "item": {"label": ("label", _text, {"required": True}),
                 "value": ("value", _decimal, {"allow_negative": True}),
                 "unit": ("unit", _text, {"max_len": 24})},
    },
    # Financial summary — per fiscal-year rows.
    #
    # `pat`, `isEstimate` and `growthPct` are declared here because the form
    # validator is a *filter*: a key it does not know is dropped on save. The
    # schema asks the model for PAT and a currency on every financial row, and
    # a live profile came back with neither on any of eleven rows — the fields
    # were being requested at one end and discarded at the other. `isEstimate`
    # matters most of all: without it nothing downstream can tell a FY30
    # projection from an audited FY24 actual, and the CKB sync was selecting
    # the projection.
    "financial_summary": {
        "kind": "fiscal_years",
        "sum_to_100": False,
        "item": {"fiscalYear": ("fiscalYear", _text, {"required": True,
                                                      "max_len": 16}),
                 "revenue": ("revenue", _decimal, {"allow_negative": True}),
                 "grossMarginPct": ("grossMarginPct", _pct, {}),
                 "growthPct": ("growthPct", _growth_pct, {}),
                 "ebitda": ("ebitda", _decimal, {"allow_negative": True}),
                 "pat": ("pat", _decimal, {"allow_negative": True}),
                 "isEstimate": ("isEstimate", _bool, {}),
                 "ccy": ("ccy", _ccy, {})},
    },
}


def validate_structured_form(section_key, payload):
    """Validate & normalise a structured form payload before it is stored.

    payload shape: {"items": [ {..}, .. ]}. Returns the normalised dict.
    Raises DomainValidationError on any bad row so the client can attach
    messages to the offending field.
    """
    form = STRUCTURED_FORMS.get(section_key)
    if not form:
        # Not a known structured form — treat as opaque JSON (no-op).
        return payload if isinstance(payload, dict) else {}

    items_in = (payload or {}).get("items")
    if items_in is None:
        items_in = []
    if not isinstance(items_in, list):
        raise DomainValidationError({"items": "Expected a list of rows."})

    items_out = []
    for i, row in enumerate(items_in):
        if not isinstance(row, dict):
            raise DomainValidationError({f"items[{i}]": "Invalid row."})
        norm = {}
        for api_field, (key, coerce, kw) in form["item"].items():
            val = coerce(row.get(api_field), f"items[{i}].{api_field}", **kw)
            # Coercers return Decimal for numbers; JSONField can only store
            # JSON-native types, so cast to float here.
            if isinstance(val, Decimal):
                val = float(val)
            norm[key] = val
        items_out.append(norm)

    if form["sum_to_100"] and items_out:
        # v27.3 — an UNWEIGHTED draft is a legitimate state.
        #
        # Most companies name their revenue streams publicly and publish no
        # split, so a model returning {label, pct: null} rows has answered
        # correctly. Rejecting the batch on "Shares must total 100%
        # (currently 0%)" discarded four correct labels and left the section
        # blank, with the error aimed at a validator that was working.
        #
        # Rows with NO percentage at all are therefore stored as a draft for
        # the user to complete. The invariant still holds for any real
        # split: as soon as one row carries a percentage, all of them must,
        # and they must total 100.
        weighted = [r for r in items_out if r.get("pct") is not None]
        if not weighted:
            return {"kind": form["kind"], "items": items_out,
                    "unweighted": True}
        if len(weighted) < len(items_out):
            raise DomainValidationError(
                {"items": f"{len(items_out) - len(weighted)} of "
                          f"{len(items_out)} rows have no percentage. Give "
                          f"every stream a share, or leave them all blank "
                          f"to save as a draft."})
        total = sum((r.get("pct") or 0) for r in items_out)
        # Allow a small rounding tolerance.
        if abs(total - 100.0) > 0.5:
            raise DomainValidationError(
                {"items": f"Shares must total 100% (currently {total:g}%)."})

    return {"kind": form["kind"], "items": items_out}
