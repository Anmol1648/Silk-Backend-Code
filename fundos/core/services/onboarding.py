"""
Onboarding surface (Gap G2 — the biggest frontend blocker).

The design assumed invite-only onboarding (GC-06), but the product cannot
onboard a brand-new customer without it. This module adds:

* signup(email, name, company_name)   — JIT tenant + user (+ company with an
  owner membership); the caller then completes the normal OTP login.
* create_company / update_company     — company profile create & edit.
* create_deal                          — a deal (round) under a company,
  gated by company ownership; provisions membership, stage states and CKB
  so the deal is immediately usable by every Stage 1–3 endpoint.
"""
import logging

from django.db import IntegrityError, transaction

from fundos.core.exceptions import AuthzError, DomainValidationError
from fundos.core.services.audit import audit

logger = logging.getLogger(__name__)

FOUNDER_ROLES = {"founder", "co_founder"}


@transaction.atomic
def signup(email: str, *, name: str = "", company_name: str = ""):
    """JIT self-signup: tenant + user ONLY. Idempotent for an existing live
    user — returns that user so the caller can proceed with the uniform OTP
    flow (never revealing whether the account already existed).

    Company auto-creation is strictly prohibited on account creation
    (company-profile-generation-flow.md §1 / §2.2). A company is created ONLY
    when the user explicitly initiates the Create Company flow
    (POST /api/v1/companies). `company_name`, if sent, is NOT turned into a
    company record here — it is used only as a human-readable label for the
    JIT tenant, so a new user correctly lands on the empty "no companies"
    state.
    """
    from fundos.core.models import Tenant, User

    email = (email or "").strip().lower()
    if not email or "@" not in email:
        raise DomainValidationError("A valid email is required.",
                                    fields={"email": "invalid"})
    existing = User.objects.filter(email=email, is_deleted=False).first()
    if existing:
        return existing, False

    tenant = Tenant.objects.create(
        name=company_name or email.split("@")[1].split(".")[0])
    user = User.objects.create_user(
        email=email, tenant_id=tenant.id,
        name=name or email.split("@")[0])
    # NOTE: intentionally NO Company / Membership creation here — see docstring.
    audit("auth.signup", actor=user, entity="user", entity_id=user.id,
          tenant_id=tenant.id, meta={"companyName": company_name})
    return user, True


def _require_company_owner(user, company):
    from fundos.core.models import Membership
    ok = Membership.objects.filter(
        user=user, scope_type="company", scope_id=company.id,
        status="active", role__in=FOUNDER_ROLES).exists()
    if not ok and str(company.created_by_id or "") != str(user.id):
        raise AuthzError()


def list_companies(user):
    from fundos.core.models import Company, Membership
    company_ids = set(
        Membership.objects.filter(user=user, scope_type="company",
                                  status="active")
        .values_list("scope_id", flat=True))
    qs = Company.objects.filter(tenant_id=user.tenant_id)
    return [c for c in qs
            if c.id in company_ids or str(c.created_by_id or "") == str(user.id)]


@transaction.atomic
def delete_company(user, company):
    """Permanently delete a company workspace and ALL associated data
    (LLM_ISSUE3, item 1: DELETE /companies/{id}).

    Owner-gated. Cascade covers:
      * deals/workspaces for the company (Deal.company is PROTECT, so deal
        children are cleared first, then the deals),
      * the company profile and every nested profile entity (Founders,
        KeyPeople, Competitors, FundingRound, NewsItem, sections, documents)
        — these CASCADE off CompanyProfile,
      * documents / materials and their PHYSICAL files in object storage
        (so no orphaned blobs remain),
      * memberships and me/contexts references (membership rows ARE the
        contexts source, so removing them stops the company appearing),
      * the company row itself.

    Storage deletion is best-effort: a blob that fails to delete is logged but
    does not abort the DB transaction, so the workspace is always removed.
    """
    _require_company_owner(user, company)

    from fundos.core.models import Company, Deal, Membership
    from fundos.profile.models import CompanyProfile

    company_id = company.id
    deal_ids = list(Deal.all_objects.filter(company_id=company_id)
                    .values_list("id", flat=True))

    # 1) Physical files first (before rows vanish), best-effort.
    _delete_storage_for_company(company_id, deal_ids)

    # 2) Deal-scoped rows across every app, then the deals themselves.
    _delete_deal_scoped_rows(deal_ids)
    Deal.all_objects.filter(company_id=company_id).delete()

    # 3) Profile + its cascade (Founders/KeyPeople/Competitors/Funding/News/
    #    sections/documents all CASCADE off CompanyProfile), and cash position.
    CompanyProfile.objects.filter(company_id=company_id).delete()
    _delete_company_scoped_rows(company_id)

    # 4) Memberships pointing at this company or its deals (contexts source).
    scope_ids = [company_id] + deal_ids
    Membership.all_objects.filter(scope_id__in=scope_ids).delete()

    # 5) The company row.
    audit("company.deleted", actor=user, entity="company",
          entity_id=company_id, tenant_id=company.tenant_id,
          meta={"deals": len(deal_ids)})
    Company.all_objects.filter(id=company_id).delete()


def _delete_storage_for_company(company_id, deal_ids):
    """Remove physical blobs for a company's documents/materials and logo."""
    try:
        from fundos.docs.storage import get_storage
        storage = get_storage()
    except Exception as e:  # storage misconfigured — skip, keep deleting rows
        logger.warning("DELETE: storage unavailable, skipping blobs: %s", e)
        return

    uris = set()
    # Document versions for the company's deals.
    try:
        from fundos.docs.models import DocumentVersion
        uris.update(
            DocumentVersion.all_objects.filter(deal_id__in=deal_ids)
            .exclude(storage_uri="").values_list("storage_uri", flat=True))
    except Exception as e:
        logger.warning("DELETE: could not enumerate document blobs: %s", e)
    # Material assets, if they carry a storage uri.
    try:
        from django.apps import apps
        materials_config = apps.get_app_config("materials")
        for model in materials_config.get_models():
            for attr in ("storage_uri", "file_uri"):
                if hasattr(model, attr):
                    mgr = getattr(model, "all_objects", None) or model._default_manager
                    uris.update(
                        mgr.filter(deal_id__in=deal_ids)
                        .exclude(**{attr: ""})
                        .values_list(attr, flat=True))
    except Exception as e:
        logger.warning("DELETE: could not enumerate material blobs: %s", e)

    for uri in uris:
        try:
            storage.delete_file(uri)
        except Exception as e:
            logger.warning("DELETE: blob %s not removed: %s", uri, e)


def _delete_deal_scoped_rows(deal_ids):
    """Delete every row keyed by these deal ids across all apps.

    Uses Django's app registry to find models with a `deal_id` UUID field or
    a `deal` FK, so a new deal-scoped model is covered automatically rather
    than being silently missed.
    """
    if not deal_ids:
        return
    from django.apps import apps

    for model in apps.get_models():
        field_names = {f.name for f in model._meta.get_fields()
                       if hasattr(f, "name")}
        mgr = getattr(model, "all_objects", None) or model._default_manager
        try:
            if "deal_id" in field_names:
                mgr.filter(deal_id__in=deal_ids).delete()
            elif "deal" in field_names:
                mgr.filter(deal_id__in=deal_ids).delete()
        except Exception as e:
            logger.warning("DELETE: %s deal-scoped purge skipped: %s",
                           model.__name__, e)


def _delete_company_scoped_rows(company_id):
    """Delete rows keyed directly by company (besides the profile cascade)."""
    from django.apps import apps

    for model in apps.get_models():
        if model._meta.label_lower in (
                "core.company",):        # deleted last, by the caller
            continue
        field_names = {f.name for f in model._meta.get_fields()
                       if hasattr(f, "name")}
        mgr = getattr(model, "all_objects", None) or model._default_manager
        try:
            if "company_id" in field_names:
                mgr.filter(company_id=company_id).delete()
            elif "company" in field_names:
                mgr.filter(company_id=company_id).delete()
        except Exception as e:
            logger.warning("DELETE: %s company-scoped purge skipped: %s",
                           model.__name__, e)


def _domain_or_blank(domain, *, exclude_id=None):
    """A normalised domain, or "" if another live company already holds it.

    `Company.domain` is unique platform-wide (`uq_company_domain_live`), not
    per tenant, so two unrelated signups reaching for the same word — a
    common one, or the same test fixture run twice — collide on it. Domain is
    a convenience field; the authoritative website lives on the profile. This
    mirrors the guard `ProfileOnboardView` already applies when it derives a
    domain from the profile's website: losing the field is far better than
    the request crashing.

    Queries through ``Company.all_objects``, deliberately bypassing the
    tenant scope ``Company.objects`` applies. The constraint this checks is
    platform-wide by design, so a check run through the tenant-scoped manager
    would be blind to any OTHER tenant's claim on the same domain — it would
    report "available" right up until the save hit the database's unique
    index, which enforces the constraint with no such blind spot. The two
    disagreeing is exactly the gap a caller's savepoint-and-retry exists to
    close; this makes that the rare path instead of the only one that works.
    """
    from fundos.core.models import Company
    candidate = (domain or "").strip().lower()
    if not candidate:
        return ""
    qs = Company.all_objects.filter(domain__iexact=candidate,
                                    is_deleted=False)
    if exclude_id:
        qs = qs.exclude(id=exclude_id)
    return "" if qs.exists() else candidate


@transaction.atomic
def create_company(user, *, name, domain="", hq_country=""):
    from fundos.core.models import Company, Membership
    if not (name or "").strip():
        raise DomainValidationError("Company name is required.",
                                    fields={"name": "required"})
    clean_name = name.strip()
    # Tester Issue 2: abandoning the wizard after Step 1 and repeating the
    # flow created duplicate company records with the same name. Company
    # creation is idempotent per (owner, name): re-submitting the same name
    # returns the existing record (and refreshes domain/HQ if now provided)
    # instead of minting a duplicate.
    existing = Company.objects.filter(
        tenant_id=user.tenant_id, name__iexact=clean_name,
        created_by=user).first()
    if existing:
        updates = []
        if domain and not existing.domain:
            candidate = _domain_or_blank(domain, exclude_id=existing.id)
            if candidate:
                existing.domain = candidate
                updates.append("domain")
        if hq_country and not existing.hq_country:
            existing.hq_country = hq_country.strip()
            updates.append("hq_country")
        if updates:
            # `_domain_or_blank`'s availability check runs through the
            # tenant-scoped default manager (`Company.objects`), so it is
            # blind to a domain another TENANT already holds — same
            # limitation `ProfileOnboardView` has always had for this
            # constraint. The savepoint is the actual safety net; the
            # pre-check only avoids paying for it in the common case.
            try:
                with transaction.atomic():
                    existing.save(update_fields=updates + ["updated_at"])
            except IntegrityError:
                # Only the domain field carries a uniqueness constraint, so
                # it is the only field that can have caused this.
                logger.warning(
                    "ONBOARDING: domain %r claimed by another tenant "
                    "between check and write for company %r — reused "
                    "without updating it.", domain, existing.name)
                existing.domain = ""
                remaining = [f for f in updates if f != "domain"]
                if remaining:
                    existing.save(update_fields=remaining + ["updated_at"])
        Membership.objects.get_or_create(
            tenant_id=user.tenant_id, user=user, scope_type="company",
            scope_id=existing.id, role="founder",
            defaults={"status": "active"})
        audit("company.reused", actor=user, entity="company",
              entity_id=existing.id, tenant_id=user.tenant_id,
              meta={"name": existing.name})
        return existing

    # A savepoint around the insert. Two requests can both pass the
    # `_domain_or_blank` check for the same domain and then race each other
    # into `create()` — the check-then-write gap is real, just narrow. Without
    # a savepoint, catching that IntegrityError here would leave this whole
    # function's outer `atomic()` block poisoned: Django refuses every further
    # query in it with "You can't execute queries until the end of the
    # 'atomic' block", which would surface as the Membership creation below
    # failing with a confusing, unrelated-looking error.
    domain_value = _domain_or_blank(domain)
    try:
        with transaction.atomic():
            company = Company.objects.create(
                tenant_id=user.tenant_id, name=clean_name,
                domain=domain_value, hq_country=(hq_country or "").strip(),
                created_by=user)
    except IntegrityError:
        logger.warning("ONBOARDING: domain %r taken between check and "
                       "write for company %r — created without it.",
                       domain_value, clean_name)
        company = Company.objects.create(
            tenant_id=user.tenant_id, name=clean_name, domain="",
            hq_country=(hq_country or "").strip(), created_by=user)
    Membership.objects.get_or_create(
        tenant_id=user.tenant_id, user=user, scope_type="company",
        scope_id=company.id, role="founder",
        defaults={"status": "active"})
    audit("company.created", actor=user, entity="company",
          entity_id=company.id, tenant_id=user.tenant_id,
          meta={"name": company.name})
    return company


@transaction.atomic
def update_company(user, company, **fields):
    _require_company_owner(user, company)
    for attr, key in (("name", "name"), ("domain", "domain"),
                      ("hq_country", "hqCountry")):
        if key in fields and fields[key] is not None:
            setattr(company, attr, str(fields[key]).strip())
    company.save()
    audit("company.updated", actor=user, entity="company",
          entity_id=company.id, tenant_id=user.tenant_id)
    return company


@transaction.atomic
def create_deal(user, company, *, name, round_type=""):
    """Create a fundraising round under a company (owner-gated) and
    provision everything the Stage 1–3 endpoints expect."""
    from fundos.core.models import Deal, Membership
    from fundos.core.services import ckb_service
    from fundos.core.services.stage_state import ensure_stage_states

    _require_company_owner(user, company)
    if not (name or "").strip():
        raise DomainValidationError("Deal name is required.",
                                    fields={"name": "required"})
    deal = Deal.objects.create(
        tenant_id=user.tenant_id, company=company, name=name.strip(),
        round_type=(round_type or "").strip().lower(),
        primary_owner=user, created_by=user)
    Membership.objects.create(
        tenant_id=user.tenant_id, user=user, scope_type="deal",
        scope_id=deal.id, role="founder", status="active")
    ensure_stage_states(deal)
    ckb_service.get_or_create_ckb(deal)
    audit("deal.created", actor=user, deal_id=deal.id, entity="deal",
          entity_id=deal.id, tenant_id=user.tenant_id,
          meta={"name": deal.name, "roundType": deal.round_type})
    from fundos.core.alerting.tasks import emit_alert
    emit_alert("fundos.deal.created", {
        "deal_id": str(deal.id), "company": company.name,
        "tenant_id": str(user.tenant_id)})
    return deal
