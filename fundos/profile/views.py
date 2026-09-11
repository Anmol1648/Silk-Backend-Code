"""
Company Profile API (PRD §5).

Company-scoped, not deal-scoped (C1): a company may have no deal, or
several concurrent ones, so the profile lives on the company.
"""
import logging
import re
from decimal import Decimal, InvalidOperation

from django.db import IntegrityError
from django.utils import timezone
from rest_framework.parsers import FormParser, MultiPartParser
from rest_framework.response import Response

from rest_framework.views import APIView

from fundos.core.exceptions import DomainValidationError, NotFoundInDeal
from fundos.core.services.audit import audit
# One definition of how a field and a section are NAMED for a reader, shared
# with the chat suggestions so the same field is never shown under two names.
from fundos.profile.labels import humanize as _humanize
from fundos.profile.labels import section_label as _section_label

logger = logging.getLogger(__name__)


def _ai_is_mocked():
    """True when LLM output is globally mocked (AppConfiguration.ai_mocked).

    Surfaced to the client so the profile can label mocked content honestly
    (Issue #6). Never raises — defaults to False if config is unavailable.
    """
    try:
        from fundos.config.feature_flags import ai_mocked
        return bool(ai_mocked())
    except Exception:
        return False


def _company_or_404(request, company_id):
    """Resolve a company the caller may access.

    Access is membership-based, exactly as for deals: a company-scoped
    membership, or a deal-scoped membership on any of the company's deals.
    Returns 404 rather than 403 so the endpoint never confirms the
    existence of a company the caller cannot see.
    """
    from django.db.models import Q

    from fundos.core.models import Company, Membership

    company = Company.objects.filter(id=company_id, is_deleted=False).first()
    if not company:
        raise NotFoundInDeal("Company not found.")
    if getattr(request.user, "is_internal", False):
        return company

    deal_ids = list(company.deals.filter(is_deleted=False)
                    .values_list("id", flat=True))
    has_access = Membership.objects.filter(
        Q(scope_type="company", scope_id=company.id)
        | Q(scope_type="deal", scope_id__in=deal_ids),
        user=request.user, is_deleted=False, status="active",
    ).exists()
    if not has_access:
        raise NotFoundInDeal("Company not found.")
    return company


def _default_deal(company, user):
    """Resolve a deal to hang deal-scoped machinery off.

    C1: a company is independent and MAY have no deal, and may have several
    concurrent ones (an equity raise and a debt raise at the same time).
    Profile work is company-scoped, but some existing infrastructure —
    generation jobs, the knowledge base, materials — is deal-scoped. This
    returns the company's active deal, creating one silently if none
    exists, without implying a company is limited to a single raise.
    """
    from fundos.core.models import Deal

    deal = (Deal.objects.filter(company=company, is_deleted=False,
                                status="active")
            .order_by("created_at").first())
    if deal:
        return deal
    return Deal.objects.create(
        tenant_id=company.tenant_id, company=company,
        name=f"{company.name} — Fundraise", status="active",
        primary_owner=user, created_by=user)


def _serialize_section(section, cfg=None):
    return {
        "sectionKey": section.section_key,
        "label": cfg.label if cfg else section.section_key,
        "kind": cfg.kind if cfg else "narrative",
        "sortOrder": cfg.sort_order if cfg else 100,
        "content": section.content,
        "structured": section.structured or {},
        "needsInput": section.needs_input,
        "missing": section.missing_notes or [],
        "versionNo": section.version_no,
        "editedByUser": section.edited_by_user,
        "isEditable": cfg.is_editable if cfg else True,
        "isRegenerable": bool(cfg and cfg.is_regenerable and cfg.llm_role),
        "requiredForCompleteness": cfg.required_for_completeness if cfg else False,
        "source": section.source,
        "confidence": float(section.confidence) if section.confidence else None,
        "verified": section.verified,
        "updatedAt": section.updated_at,
    }


class ProfileView(APIView):
    """GET the whole profile with all sections."""

    def get(self, request, company_id):
        """Return the profile in the clean external contract (Gap doc
        §6/§7/§8/§10.2, and Gap2 cleanup).

        ``sections`` is an OBJECT keyed by the 14 spec section keys, each
        value wrapped as {sectionKey, isComplete, lastUpdatedAt, data}. The
        duplicate top-level entity arrays are gone — that data lives inside
        each section's ``data``.

        Gap2 changes:
          * The legacy ``sectionsEditor`` array and the ``editorRecords`` map
            are REMOVED (Open Item #13): the frontend consumes only the clean
            ``sections`` object now, so the response no longer ships a second
            and third copy of the same data.
          * ``readiness`` (real AI output that has no home in the 14-section
            schema) is surfaced as its own top-level object rather than being
            stranded in the old editor array.
          * ``documents`` (the §5.2 Document Center shape) stays top-level.
        """
        from fundos.profile.models import ProfileDocument, ProfileSection
        from fundos.profile.services import get_or_create_profile, ensure_sections
        from fundos.profile.spec_serializer import serialize_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        ensure_sections(profile)

        payload = serialize_profile(
            profile, company,
            default_deal_id=_default_deal(company, request.user).id,
            ai_mocked=_ai_is_mocked())

        # Document Center listing (§5.2 shape) — legitimately top-level.
        payload["documents"] = [
            _document_payload(d)
            for d in ProfileDocument.objects.filter(profile=profile)]

        # Investor Readiness is substantial AI output with no slot in the
        # 14-section schema. Give it the SAME §8 wrapper as every other
        # section — data:{summary} — so the shape is consistent (issue #3).
        readiness = ProfileSection.objects.filter(
            profile=profile, section_key="readiness", is_active=True).first()
        payload["readiness"] = {
            "sectionKey": "readiness",
            "isComplete": bool(readiness and readiness.content
                               and not readiness.needs_input),
            "lastUpdatedAt": (readiness.updated_at.isoformat()
                              if readiness and readiness.updated_at else None),
            "data": {"summary": (readiness.content if readiness else "") or ""},
        }

        return Response(payload)


def _document_payload(d):
    """Document Center entry, including a download link.

    C7 requires migrated Stage-3 material to stay *accessible* read-only.
    Previously only the filename was returned, so the UI could list a
    teaser but had no way to open it. Resolve the current version's stored
    file and sign it; a document with nothing stored simply has no link.
    """
    download_url = None
    if d.document_id:
        try:
            from fundos.docs.models import Document, DocumentVersion
            from fundos.docs.storage import get_storage

            document = Document.objects.filter(id=d.document_id).first()
            version = None
            if document and document.current_version_id:
                version = DocumentVersion.objects.filter(
                    id=document.current_version_id).first()
            if version is None and document:
                version = (DocumentVersion.objects
                           .filter(document_id=document.id)
                           .order_by("-version_no").first())
            if version and version.storage_uri:
                download_url = get_storage().signed_url(version.storage_uri)
        except Exception:      # never let a storage hiccup break the page
            download_url = None
    return {
        "id": str(d.id), "category": d.category,
        "filename": d.filename, "isReadonly": d.is_readonly,
        "documentId": str(d.document_id) if d.document_id else None,
        "originStage": d.origin_stage,
        "downloadUrl": download_url,
    }


class CompanyDefaultDealView(APIView):
    """Resolve the deal that company-scoped screens should work against.

    Silk 2.0 hides the deal from the founder, but the strategy machinery
    (targets, buckets, cash) is still deal-scoped. The Company Profile's
    "Continue to Fundraising Strategy" had no way to learn which deal to
    use, so the strategy page rendered without a deal context and crashed
    on `dealId` (QA 24-Jul issues 6 and 7). This endpoint gives the client
    the same answer the server already uses internally, creating the deal
    only if the company genuinely has none.
    """

    def get(self, request, company_id):
        company = _company_or_404(request, company_id)
        deal = _default_deal(company, request.user)
        return Response({
            "dealId": str(deal.id),
            "dealName": deal.name,
            "companyId": str(company.id),
            "companyName": company.name,
        })



class CompanyDocumentsView(APIView):
    """Upload a document into the Company Profile's Document Center.

    QA 24-Jul issue 4: a profile created without documents had no way to add
    them afterwards — the Document Center listed "No documents yet" with no
    upload control, so the founder had to redo onboarding. Uploading is
    company-scoped like the rest of the profile; storage, virus scanning and
    versioning reuse the existing material pipeline behind the company's
    working deal.
    """
    parser_classes = [MultiPartParser, FormParser]

    ALLOWED_CATEGORIES = {
        "company_presentation", "financial_model", "annual_report",
        "business_plan", "other",
    }

    def post(self, request, company_id):
        from fundos.profile.models import ProfileDocument
        from fundos.profile.services import get_or_create_profile
        from fundos.readiness.services import register_material

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)

        upload = request.FILES.get("file")
        if not upload:
            raise DomainValidationError({"file": "Choose a file to upload."})

        category = (request.data.get("category") or "other").strip()
        if category not in self.ALLOWED_CATEGORIES:
            category = "other"

        deal = _default_deal(company, request.user)
        material, document = register_material(
            deal, filename=upload.name, content=upload.read(),
            mime=upload.content_type or "",
            category=category, user=request.user)

        entry = ProfileDocument.objects.create(
            tenant_id=company.tenant_id, profile=profile,
            category=category, filename=upload.name,
            document_id=document.id,
            # `register_material` has handed us the material all along and
            # the column has existed since the first migration; nothing ever
            # wrote it. So `_record_extraction` bailed on a null id and every
            # Document Center row reported an empty handler, ocr_status and
            # ocr_reason — the three fields that answer "was my file read?".
            material_asset_id=getattr(material, "id", None),
            is_readonly=False,
            origin_stage="document_center", created_by=request.user)
        audit("profile.document_uploaded", actor=request.user,
              entity="profile_document", entity_id=entry.id,
              tenant_id=company.tenant_id,
              meta={"companyId": str(company.id), "category": category})
        from fundos.profile.services import recompute_structured_sections
        recompute_structured_sections(profile)   # clears the Doc Center dot
        return Response(_document_payload(entry), status=201)

    def delete(self, request, company_id, document_id=None):
        from fundos.profile.models import ProfileDocument

        company = _company_or_404(request, company_id)
        entry = ProfileDocument.objects.filter(
            id=document_id, profile__company=company).first()
        if not entry:
            raise NotFoundInDeal("Document not found.")
        if entry.is_readonly:
            raise DomainValidationError({
                "document": "This document was generated by an earlier "
                            "stage and is read-only."})
        profile = entry.profile
        entry.delete()
        from fundos.profile.services import recompute_structured_sections
        recompute_structured_sections(profile)
        return Response(status=204)



class ProfileOnboardView(APIView):
    """POST the single onboarding form (PRD §5.2) and start generation."""

    def post(self, request, company_id):
        from fundos.core.services.jobs import create_job
        from fundos.platformcfg.services import home_currency_for_country
        from fundos.profile.models import Founder
        from fundos.profile.services import get_or_create_profile
        from fundos.profile.tasks import generate_profile_task

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)

        website = (request.data.get("websiteUrl") or "").strip()
        if not website:
            raise DomainValidationError({"websiteUrl": "A company website is required."})
        country = (request.data.get("hqCountry") or "").strip().upper()[:2]
        if not country:
            raise DomainValidationError({"hqCountry": "Headquarters country is required."})

        profile.website_url = website
        # CR-01 — the company's own LinkedIn page, optional. Validated when
        # supplied so a typo is caught at entry rather than producing a dead
        # link on the dashboard.
        company_linkedin = (request.data.get("linkedinUrl")
                            or request.data.get("companyLinkedinUrl") or "").strip()
        if company_linkedin:
            from fundos.profile.section_writer import _as_url, SectionUpdateError
            try:
                profile.linkedin_url = _as_url("profile", "linkedin_url",
                                               company_linkedin)
            except SectionUpdateError:
                raise DomainValidationError(
                    {"linkedinUrl": "Enter a valid LinkedIn URL, for example "
                                    "https://linkedin.com/company/acme"})
        profile.hq_country = country
        profile.home_currency = (request.data.get("homeCurrency")
                                 or home_currency_for_country(country))
        # Optional company logo (LLM_ISSUE3 item 3): a direct URL stored as-is,
        # or a base64 upload persisted to storage. Never fails onboarding.
        from fundos.profile.logo import resolve_logo
        logo = resolve_logo(profile,
                            logo_url=request.data.get("logoUrl") or "",
                            logo_base64=request.data.get("logoBase64") or "")
        if logo:
            profile.logo_url = logo
        profile.save(update_fields=["website_url", "linkedin_url",
                                    "hq_country", "home_currency", "logo_url",
                                    "updated_at"])

        if not company.domain:
            candidate = website.replace("https://", "").replace(
                "http://", "").rstrip("/")
            # `company.domain` is unique across live companies. Another
            # company (often an earlier abandoned attempt) may already hold
            # this domain, and saving it blind raised IntegrityError — which
            # surfaced as an intermittent HTTP 500 during profile generation
            # that "worked on retry" (QA 24-Jul issue 5). The domain is a
            # convenience field; the authoritative website lives on the
            # profile, so a clash simply leaves it unset.
            from fundos.core.models import Company as _Company
            taken = (_Company.objects.filter(domain__iexact=candidate,
                                             is_deleted=False)
                     .exclude(id=company.id).exists())
            if not taken:
                company.domain = candidate
        company.hq_country = country
        try:
            company.save(update_fields=["domain", "hq_country", "updated_at"])
        except IntegrityError:
            # Lost a race for the same domain between the check and the
            # write. Keep going without it rather than failing the run.
            logger.warning("PROFILE: domain clash for company %s — skipped",
                           company.id)
            company.refresh_from_db()
            company.hq_country = country
            company.save(update_fields=["hq_country", "updated_at"])

        # Founders — C3: LinkedIn optional; confirmation recorded when given.
        # Re-submitting the form replaces the founder set: appending blindly
        # duplicated every founder on each retry.
        founders = request.data.get("founders") or []
        supplied = [f for f in founders
                    if (f.get("name") or "").strip()
                    or (f.get("linkedinUrl") or "").strip()]
        if supplied:
            Founder.objects.filter(profile=profile, source="founder").delete()
        for i, item in enumerate(supplied):
            url = (item.get("linkedinUrl") or "").strip()
            name = (item.get("name") or "").strip()
            confirmed = bool(item.get("selfConfirmed"))
            Founder.objects.create(
                tenant_id=company.tenant_id, profile=profile,
                name=name, linkedin_url=url,
                designation=(item.get("designation") or "").strip(),
                is_full_time=bool(item.get("isFullTime", True)),
                months_full_time=item.get("monthsFullTime"),
                self_confirmed=confirmed,
                self_confirmed_at=timezone.now() if confirmed else None,
                source="founder", sort_order=i, created_by=request.user)

        # Generation is tracked as a job so the UI can poll sub-progress.
        # GenerationJob is deal-scoped, so attach it to the company's default
        # deal (created implicitly at company creation — see onboarding).
        deal = _default_deal(company, request.user)
        job = create_job(deal, "profile_generate", request.user)
        profile.generation_job_id = job.id
        profile.save(update_fields=["generation_job_id", "updated_at"])

        # ?force=true (or {"force": true}) bypasses the skip-if-unchanged
        # gate. Default is off so a repeated submit of identical sources
        # does not re-bill an identical generation.
        force = str(request.data.get("force",
                                     request.query_params.get("force", ""))
                    ).lower() in ("1", "true", "yes")
        generate_profile_task.delay(str(profile.id), str(job.id),
                                    str(request.user.id), force)
        audit("profile.onboarding_submitted", actor=request.user,
              entity="company_profile", entity_id=profile.id,
              tenant_id=company.tenant_id,
              meta={"companyId": str(company.id)})
        return Response({"jobId": str(job.id), "status": "generating"},
                        status=202)


class ProfileSectionView(APIView):
    """PATCH a section (edit + save) — standardized contract.

    Body is always ``{"data": <section's data shape>}`` (the exact shape the
    GET response returns for that section, object or array). The change is
    PERSISTED, isComplete is recalculated, and the response echoes back the
    §8 section wrapper so the client needs no follow-up GET.

    The older ``{"content": ..., "structured": ...}`` body is still accepted
    for backward compatibility, but ``data`` is the contract going forward.
    """

    def patch(self, request, company_id, section_key):
        from fundos.profile.models import ProfileSection
        from fundos.profile.services import get_or_create_profile, update_section
        from fundos.profile.section_writer import (
            SectionUpdateError, update_section_from_data,
        )
        from fundos.profile.spec_serializer import serialize_section

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)

        if "data" in request.data:
            # Standardized contract (section-data-integrity doc §3).
            try:
                update_section_from_data(
                    profile, section_key, request.data.get("data"),
                    user=request.user)
            except SectionUpdateError as e:
                return Response({"detail": str(e)}, status=422)
        elif ("content" in request.data) or ("structured" in request.data):
            # Legacy body — kept working so existing callers don't break.
            update_section(
                profile, section_key,
                content=request.data.get("content"),
                structured=request.data.get("structured"),
                user=request.user,
                change_summary=request.data.get("changeSummary", "Edited"),
                needs_input=False)
        # A body carrying only `confirmed_fields` has nothing to write, and
        # calling the legacy writer for it did real damage: `update_section`
        # takes the WIRE key, so a confirm on `company_profile` created a
        # SECOND, empty row beside the real `company_overview` one. Both map
        # back to the same section on read, the empty one won, and the
        # confirmation the founder had just given vanished — with a 200 and
        # no error to explain it.

        # Req 3: persist confirmed_fields if present in the payload.
        if "confirmed_fields" in request.data:
            from fundos.profile.spec_serializer import storage_key_for
            internal_key = storage_key_for(section_key)
            sec = ProfileSection.objects.filter(
                profile=profile, section_key=internal_key,
                is_active=True).first()
            if sec is None:
                # A section can hold data without ever holding a narrative
                # row — founders live in their own tables, and so do several
                # others. Dropping the confirmation in that case returned 200
                # to a founder who had just clicked Confirm and saved
                # nothing, which is the worst shape a failure can take: the
                # UI shows it worked, the score does not move, and there is
                # no error anywhere to explain the difference.
                sec = ProfileSection.objects.create(
                    profile=profile, section_key=internal_key,
                    tenant_id=profile.tenant_id, is_active=True)
            cf = request.data["confirmed_fields"]
            sec.confirmed_fields = cf if isinstance(cf, list) else []
            sec.save(update_fields=["confirmed_fields", "updated_at"])

        audit("profile.section_edited", actor=request.user,
              entity="company_profile_section", entity_id=profile.id,
              tenant_id=company.tenant_id,
              meta={"sectionKey": section_key})

        # Echo the freshly-saved section in the exact §8 GET shape,
        # now including confirmed_fields and recalculated score.
        profile.refresh_from_db()
        result = serialize_section(profile, section_key)

        # Req 4: include top-level score + readiness_stage + breakdown after
        # every PATCH so the frontend can update the progress bar.
        from fundos.profile.spec_serializer import (
            build_sections, _readiness_score, _readiness_stage,
            _readiness_breakdown, _readiness_totals,
        )
        from fundos.profile import suggestions
        sections = build_sections(profile)
        # The same per-section prompts the profile GET returns, so the chat
        # beside a freshly-saved section offers what that save left undone.
        breakdown = suggestions.attach(_readiness_breakdown(sections))
        score = _readiness_score(sections)
        result["score"] = score
        result["readiness_stage"] = _readiness_stage(score)
        result["readinessBreakdown"] = breakdown
        result["readinessTotals"] = _readiness_totals(breakdown)

        return Response(result)


class ProfileSectionRegenerateView(APIView):
    """POST — regenerate one section with AI."""

    def post(self, request, company_id, section_key):
        from fundos.profile.services import get_or_create_profile, regenerate_section

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        result = regenerate_section(
            profile, section_key, user=request.user,
            force=bool(request.data.get("force")))
        return Response(result)


class ProfileStructuredFormView(APIView):
    """PATCH a numeric/structured form (Revenue Model, Company Metrics,
    Financial Summary).

    The payload is {"items": [...]}. Validation (typed numbers, revenue
    shares totalling 100%) runs server-side in
    records.validate_structured_form; a bad row returns 422 with a
    field-attachable message so the client can highlight it. Stored on
    ProfileSection.structured, versioned through the standard update path.
    """

    def patch(self, request, company_id, section_key):
        from fundos.profile.services import (get_or_create_profile,
                                             update_structured_form)

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        section = update_structured_form(
            profile, section_key,
            {"items": request.data.get("items", [])},
            user=request.user)
        audit("profile.structured_form_saved", actor=request.user,
              entity="company_profile_section", entity_id=section.id,
              tenant_id=company.tenant_id,
              meta={"sectionKey": section_key})
        return Response({"sectionKey": section_key,
                         "structured": section.structured,
                         "versionNo": section.version_no})


class ProfileFieldRegenerateView(APIView):
    """POST — regenerate a single field/row of a structured section.

    Field-level regeneration is a new capability the enhancement set asks
    for explicitly. Returns the suggested value only; the client decides
    whether to accept it into the form (the founder's other fields are
    never touched server-side).
    """

    def post(self, request, company_id, section_key, field_key):
        from fundos.profile.services import (get_or_create_profile,
                                             regenerate_field)

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        result = regenerate_field(profile, section_key, field_key,
                                  user=request.user)
        return Response(result)


class ProfileSectionHistoryView(APIView):
    def get(self, request, company_id, section_key):
        from fundos.profile.models import ProfileSection, ProfileSectionVersion
        from fundos.profile.services import get_or_create_profile
        from fundos.profile.spec_serializer import storage_key_for

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        # Resolve the wire key to the stored key. Without this, history for
        # the sections renamed on the API surface (investors_and_cap_table,
        # company_story_and_usp, industry_and_market_research) and for
        # company_profile — stored as company_overview — always came back
        # empty, because the lookup used a key that is never stored.
        section = ProfileSection.objects.filter(
            profile=profile, section_key=storage_key_for(section_key),
            is_active=True).first()
        if not section:
            return Response({"items": []})
        rows = ProfileSectionVersion.objects.filter(section=section)
        return Response({"items": [{
            "versionNo": r.version_no,
            "content": r.content,
            "changeSummary": r.change_summary,
            "changedAt": r.changed_at,
            "changedBy": r.changed_by.email if r.changed_by else None,
        } for r in rows]})


class ProfileReviewView(APIView):
    """POST — the owner confirms the profile has been reviewed (C2)."""

    def post(self, request, company_id):
        from fundos.profile.services import confirm_review, get_or_create_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        confirm_review(profile, request.user)
        audit("profile.review_confirmed", actor=request.user,
              entity="company_profile", entity_id=profile.id,
              tenant_id=company.tenant_id)
        return Response({"reviewedAt": profile.reviewed_at,
                         "isComplete": profile.is_complete})


class FoundersView(APIView):
    """POST add a founder."""

    def post(self, request, company_id):
        from fundos.profile.models import Founder
        from fundos.profile.services import get_or_create_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        confirmed = bool(request.data.get("selfConfirmed"))
        founder = Founder.objects.create(
            tenant_id=company.tenant_id, profile=profile,
            name=(request.data.get("name") or "").strip(),
            designation=(request.data.get("designation") or "").strip(),
            linkedin_url=(request.data.get("linkedinUrl") or "").strip(),
            experience=request.data.get("experience", ""),
            education=request.data.get("education", ""),
            biography=request.data.get("biography", ""),
            is_full_time=bool(request.data.get("isFullTime", True)),
            months_full_time=request.data.get("monthsFullTime"),
            self_confirmed=confirmed,
            self_confirmed_at=timezone.now() if confirmed else None,
            source="founder", created_by=request.user)
        from fundos.profile.services import recompute_structured_sections
        recompute_structured_sections(profile)   # clears the Founders dot
        return Response({"id": str(founder.id)}, status=201)


class FounderDetailView(APIView):
    def patch(self, request, company_id, founder_id):
        from fundos.profile.models import Founder
        from fundos.profile.services import get_or_create_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        founder = Founder.objects.filter(id=founder_id, profile=profile).first()
        if not founder:
            raise NotFoundInDeal("Founder not found.")
        for field, attr in (("name", "name"), ("designation", "designation"),
                            ("linkedinUrl", "linkedin_url"),
                            ("experience", "experience"),
                            ("education", "education"),
                            ("biography", "biography")):
            if field in request.data:
                setattr(founder, attr, request.data[field])
        if "isFullTime" in request.data:
            founder.is_full_time = bool(request.data["isFullTime"])
        if "monthsFullTime" in request.data:
            founder.months_full_time = request.data["monthsFullTime"]
        if request.data.get("selfConfirmed"):
            founder.self_confirmed = True
            founder.self_confirmed_at = timezone.now()
        founder.save()
        return Response({"id": str(founder.id)})

    def delete(self, request, company_id, founder_id):
        from fundos.profile.models import Founder
        from fundos.profile.services import get_or_create_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        founder = Founder.objects.filter(id=founder_id, profile=profile).first()
        if not founder:
            raise NotFoundInDeal("Founder not found.")
        founder.delete(deleted_by=request.user)
        from fundos.profile.services import recompute_structured_sections
        recompute_structured_sections(profile)
        return Response(status=204)


class CashPositionView(APIView):
    """GET / POST the cash position (PRD §6 Step 2, C5)."""

    def get(self, request, company_id):
        from fundos.profile.models import CashPosition

        company = _company_or_404(request, company_id)
        row = CashPosition.objects.filter(company=company,
                                          is_active=True).first()
        if not row:
            return Response({"cashInBank": None, "monthlyBurn": None})
        return Response(_cash_payload(row))

    def post(self, request, company_id):
        from fundos.profile.services import (
            compute_cash_position, get_or_create_profile,
        )

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        ccy = request.data.get("ccy") or profile.home_currency or "USD"
        as_of = request.data.get("asOfDate")
        row = compute_cash_position(
            company,
            cash_value=request.data.get("cashInBank"),
            cash_ccy=ccy,
            burn_value=request.data.get("monthlyBurn"),
            burn_ccy=ccy,
            as_of_date=as_of,
            deal_id=request.data.get("dealId"),
            desired_runway_months=request.data.get("desiredRunwayMonths"),
            user=request.user)
        return Response(_cash_payload(row))


def _cash_payload(row):
    # C5 explicitly allows a zero burn (the company may be profitable), and
    # a zero cash balance is equally real. `if value` treats both as absent,
    # which erased a saved 0 the next time the form loaded — so test against
    # None, not truthiness.
    def num(v):
        return float(v) if v is not None else None

    return {
        "cashInBank": num(row.cash_in_bank_value),
        "monthlyBurn": num(row.monthly_burn_value),
        "ccy": row.cash_in_bank_ccy,
        "asOfDate": row.as_of_date,
        "isProfitable": row.is_profitable,
        "runwayMonths": num(row.runway_months),
        "cashExhaustionDate": row.cash_exhaustion_date,
        "additionalCapitalRequiredUsd": num(
            row.additional_capital_required_usd),
        "fxRateUsed": num(row.fx_rate_used),
        "fxAsOf": row.fx_as_of,
    }


class FundraiseRecommendationView(APIView):
    """GET the default fund-raise bucket and the reasoning (C9)."""

    def get(self, request, company_id):
        from fundos.platformcfg.services import buckets
        from fundos.profile.services import (
            get_or_create_profile, recommend_fundraise,
        )

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        recommendation = recommend_fundraise(company, profile)
        return Response({
            "recommended": recommendation,
            "allBuckets": [{
                "bucketNo": b.bucket_no, "label": b.label,
                "amountUsd": float(b.amount_usd),
                "typicalStage": b.typical_stage,
                "investorTypes": b.investor_types,
                "dilutionLowPct": float(b.typical_dilution_low_pct),
                "dilutionHighPct": float(b.typical_dilution_high_pct),
            } for b in buckets()],
        })


class DealTargetsView(APIView):
    """Manual headline terms for a raise (C6).

    GET  → current targets, with the calculated pair alongside the inputs.
    POST → save the two inputs; post-money and dilution are derived.

    A founder or advisor who already knows their numbers can use this to
    skip the guided strategy entirely and still satisfy everything
    downstream — the stage process is not mandatory, the outputs are.
    """

    def get(self, request, company_id):
        from fundos.profile.services import get_deal_targets

        company = _company_or_404(request, company_id)
        deal_id = request.query_params.get("dealId")
        deal = (_deal_or_404(company, deal_id) if deal_id
                else _default_deal(company, request.user))

        row = get_deal_targets(deal.id)
        return Response({
            "dealId": str(deal.id),
            "dealName": deal.name,
            "targets": row.as_payload() if row else None,
        })

    def post(self, request, company_id):
        from fundos.profile.services import save_deal_targets

        company = _company_or_404(request, company_id)
        deal_id = request.data.get("dealId")
        deal = (_deal_or_404(company, deal_id) if deal_id
                else _default_deal(company, request.user))

        target_raise = _positive_or_none(
            request.data.get("targetRaise"), "targetRaise")
        pre_money = _positive_or_none(
            request.data.get("preMoneyValuation"), "preMoneyValuation")

        if target_raise is None and pre_money is None:
            raise DomainValidationError({
                "targetRaise": "Enter a target raise and a pre-money "
                               "valuation. Post-money and dilution are "
                               "calculated from them.",
            })

        row = save_deal_targets(
            company, deal.id,
            target_raise=target_raise,
            pre_money=pre_money,
            ccy=request.data.get("ccy"),
            notes=request.data.get("notes", ""),
            user=request.user,
            source=request.data.get("source") or "founder")

        audit("strategy.targets_entered", actor=request.user, deal_id=deal.id,
              entity="deal_targets", entity_id=row.id,
              meta={"source": row.source})

        return Response({"dealId": str(deal.id),
                         "targets": row.as_payload()})


def _positive_or_none(raw, field):
    """Numeric coercion with a clear message. Blank means 'not supplied'."""
    if raw in (None, ""):
        return None
    try:
        value = Decimal(str(raw))
    except (InvalidOperation, TypeError, ValueError):
        raise DomainValidationError({field: "Enter a number."})
    if value < 0:
        raise DomainValidationError({field: "Enter a positive amount."})
    return value


def _deal_or_404(company, deal_id):
    from fundos.core.models import Deal

    deal = Deal.objects.filter(id=deal_id, company=company,
                               is_deleted=False).first()
    if not deal:
        raise NotFoundInDeal("Deal not found for this company.")
    return deal


class ProfileDeepGenerateView(APIView):
    """POST — run the full company-profile pipeline, forced.

    This used to invoke a one-shot deep extract that has been replaced by the
    Company Master Data Pipeline (research the company across the question
    bank, read its documents, merge both into a dossier, derive the profile
    from that dossier). The endpoint is retained because clients call it, and
    it keeps its distinguishing behaviour: it always runs, bypassing the
    sources-unchanged skip gate, which is what "deep generate" meant to a
    caller reaching for it.

    Runs synchronously. The pipeline is materially longer than the call it
    replaces — ten grounded research calls rather than one — so a client
    should prefer the async ``POST /profile/onboard`` path and poll, and treat
    this as the force-a-rebuild button it now is.
    """

    def post(self, request, company_id):
        from fundos.profile.services import (generate_profile,
                                             get_or_create_profile)
        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        result = generate_profile(profile, user=request.user, force=True)
        audit("profile.deep_generated", actor=request.user,
              entity="company_profile", entity_id=profile.id,
              tenant_id=company.tenant_id)
        return Response(result)


class ProfileRunsView(APIView):
    """GET — generation runs for this company, newest first.

    A run is the unit a founder waits on and an operator debugs, and until now
    it had no representation on the API at all: the only signal was the
    profile's `status` field flipping from generating to generated, which
    cannot distinguish "still researching" from "wedged" from "finished twenty
    minutes ago".
    """

    def get(self, request, company_id):
        from fundos.profile.models import ProfileGenerationRun
        from fundos.profile.services import get_or_create_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        runs = (ProfileGenerationRun.objects.filter(profile=profile)
                .order_by("-created_at")[:20])
        return Response({"items": [_run_summary(r) for r in runs]})


class ProfileRunDetailView(APIView):
    """GET — everything about one generation run.

    One response rather than several endpoints, so a client polling a
    twenty-minute run makes one request regardless of how far along it is:
    lifecycle, per-stage timings, the activity log, what each uploaded document
    contributed, and a signed link to the dossier the profile was built from.
    """

    def get(self, request, company_id, run_id):
        from fundos.profile.models import ProfileGenerationRun
        from fundos.profile.services import get_or_create_profile

        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        run = ProfileGenerationRun.objects.filter(
            id=run_id, profile=profile).first()
        if run is None:
            return Response({"detail": "Run not found."}, status=404)

        payload = _run_summary(run)
        payload.update({
            "stageTimings": run.stage_timings or {},
            "progress": run.progress or {},
            "sourceStats": run.source_stats or {},
            "sourceFailures": run.source_failures or [],
            "sectionsGenerated": run.sections_generated or [],
            "sectionsNeedingInput": run.sections_needing_input or [],
            "sectionsSkipped": run.sections_skipped or [],
            "documents": run.documents or [],
            "activity": [
                {"at": e.at.isoformat(), "tPlusSeconds": e.t_plus_seconds,
                 "stage": e.stage, "message": e.message}
                for e in run.events.all()
            ],
            "dossier": _dossier_payload(run),
            "error": run.error or None,
        })
        return Response(payload)


def _run_summary(run):
    """The small shape — safe to return many of."""
    from fundos.profile.pipeline.tracker import STAGE_LABELS

    return {
        "runId": str(run.id),
        "status": run.status,
        "stage": run.stage,
        "stageLabel": STAGE_LABELS.get(run.stage, run.stage),
        "mode": run.mode,
        "forced": run.forced,
        "createdAt": run.created_at.isoformat() if run.created_at else None,
        "startedAt": run.started_at.isoformat() if run.started_at else None,
        "finishedAt": run.finished_at.isoformat() if run.finished_at else None,
        "durationMs": run.duration_ms,
        "batchesTotal": run.batches_total,
        "batchesSucceeded": run.batches_succeeded,
        "batchesFailed": run.batches_failed or [],
        "dossierChars": run.dossier_chars,
        "completenessPct": float(run.completeness_pct or 0),
        "llmCalls": run.llm_calls,
        "searches": run.total_search_count,
    }


def _dossier_payload(run):
    """A short-lived signed link to the run's evidence file.

    Generated per request rather than stored, because the URL expires and a
    stored one would be a link that works until it silently does not.
    """
    if not run.dossier_uri:
        return None
    try:
        from fundos.docs.storage import get_storage
        url = get_storage().signed_url(run.dossier_uri, expires_sec=900)
    except Exception as exc:
        logger.warning("PROFILE: could not sign dossier URL for run %s: %s",
                       run.id, exc)
        return {"characters": run.dossier_chars, "downloadUrl": None,
                "error": "The dossier could not be linked for download."}
    return {"characters": run.dossier_chars, "downloadUrl": url,
            "format": "md", "expiresInSeconds": 900}


#: A citation naming a FILE ends in an extension; a citation naming a research
#: batch does not. The same discriminator the dossier uses to harvest its
#: labels, so the two cannot disagree about what counts as a document.
_FILENAME_SOURCE = re.compile(r"\.[A-Za-z0-9]{2,5}$")


#: How much of a locator belongs on a one-line title. `Page 12` and
#: `Consolidated P&L, Row 22` fit with room to spare; a research question does
#: not, and is served whole in `locator` for anyone who wants it.
_LOCATOR_IN_TITLE = 48


def _short_locator(locator):
    """The locator as it reads on a title line."""
    text = str(locator or "").strip()
    if len(text) <= _LOCATOR_IN_TITLE:
        return text
    return text[:_LOCATOR_IN_TITLE].rstrip(" ,;:-") + "…"


def _field_label(profile, wire_key, address):
    """What to call this field on screen.

    An array item is addressed by a UUID, which is the right address and an
    unshowable name: the response carried `445a49fc-e94c-431e-bda5-…` and
    nothing a reader could recognise, so the UI had to already know what it
    had asked about.

    The label comes from the item's OWN identifying field — the same one the
    writer matches citations by — so the name shown and the identity the
    citation was attached to are one thing and cannot drift. Falls back to the
    address, never to blank: an unnamed row is still addressable.
    """
    from fundos.profile import schema as profile_schema
    from fundos.profile.pipeline.writer import _IDENTIFYING_FIELDS
    from fundos.profile.spec_serializer import serialize_section

    if not address:
        return ""
    spec = profile_schema.sections_by_key().get(wire_key) or {}
    try:
        served = serialize_section(profile, wire_key).get("data")
    except Exception:                       # pragma: no cover - never fatal
        served = None

    # A WRAPPER section declares one field whose value is the list -- the
    # Document Center's `documents`. Its items are addressed by their own ids
    # exactly like a plain array's, so they have to be searched the same way,
    # or every document comes back named by its UUID.
    if isinstance(served, dict) and len(served) == 1:
        inner = next(iter(served.values()))
        if isinstance(inner, list):
            served = inner
    if not isinstance(served, list):
        return _humanize(address) if spec.get("kind") != "array" else address

    for item in served:
        if not isinstance(item, dict) or str(item.get("id")) != address:
            continue
        for field in _IDENTIFYING_FIELDS:
            value = item.get(field)
            if isinstance(value, str) and value.strip():
                return value.strip()
        return address
    return address


def _document_analysis(profile, item_id):
    """What we know about one uploaded document, as its own provenance.

    A document IS the source, so asking where it came from has no interesting
    answer — but asking what we made of it has several, and every one of them
    was already generated and stored. The Document Center reported `cited:
    false` for a file the pipeline had read, transcribed and critiqued.

    Returns ``(sources, analysis)``: the file itself as the citation, and the
    critique beside it.
    """
    from fundos.profile.models import ProfileDocument
    from fundos.readiness.models import MaterialAsset

    row = ProfileDocument.objects.filter(
        profile=profile, id=item_id).first()
    if not row:
        return [], None

    material = (MaterialAsset.objects.filter(id=row.material_asset_id).first()
                if row.material_asset_id else None)
    if material is None and row.document_id:
        # Older rows have no link because nothing wrote one, but both sides
        # carry the same `document_id`. Resolving on that means every document
        # already uploaded gets its analysis without a backfill.
        material = MaterialAsset.objects.filter(
            document_id=row.document_id).order_by("-created_at").first()

    # How this file was actually read, in the extractor's own words. When a
    # file genuinely cannot be read, that has to be legible as this file's
    # outcome rather than as generic advice — "upload a searchable export"
    # over a deck nobody tried to open is what made the panel untrustworthy.
    reason = getattr(material, "ocr_reason", "") or ""
    source = {
        "id": "src_1",
        "name": row.filename or "Uploaded document",
        "locator": "",
        "title": row.filename or "Uploaded document",
        "type": "document",
        "snippet": reason,
        "url": "",
    }
    if material is None:
        return [source], None

    analysis = {
        "detectedType": material.detected_type or "",
        "summary": material.summary or "",
        "missingInformation": list(material.missing_information or []),
        "inconsistencies": list(material.inconsistencies or []),
        "recommendations": list(material.recommendations or []),
        "metrics": list(material.extracted_metrics or []),
        # The extraction outcome, so "we found nothing in this document" and
        # "we could not open this document" stop reading the same.
        "extraction": {
            "status": material.extraction_status or "pending",
            "handler": getattr(material, "handler", "") or "",
            "readByModel": bool(material.ocr_applied),
            "reason": reason,
        },
    }
    return [source], analysis


#: At most this many citations behind one field. A P&L cited cell by cell can
#: run to dozens of near-identical rows; a reader needs enough to see where the
#: figure came from, not the whole workbook.
_MAX_CITATIONS_PER_FIELD = 12


def _sort_key(address):
    """Order dotted addresses the way the data reads: `financials.2` after
    `financials.10` is wrong, and `0` before `10` needs the number."""
    parts = []
    for segment in str(address).split("."):
        parts.append((0, int(segment), "") if segment.isdigit()
                     else (1, 0, segment))
    return parts


def _citations_for(store, address):
    """Every citation recorded for one field, exact match or one level in.

    The model cites INTO the structure it just wrote. Asked for the provenance
    of `financial_summary.financials` it does not answer once about the table;
    it answers about `financials.0.pat_m` and `observations.3` and
    `investors_list.4.date` — which is a better answer to a question the UI
    never asks, because the UI addresses whole fields.

    So Tyreplex stored 54 citations for `financial_summary` and 61 for the cap
    table and served `cited: false` for both: real, paid-for, verbatim-quoted
    evidence that overlapped the requested address exactly nowhere.

    Matching the first segment collects them under the field a reader actually
    clicks, and the endpoint already returns a list, so `financials` shows the
    rows it was built from rather than one of them. Duplicates are dropped —
    twelve cells out of one P&L row cite that row twelve times.
    """
    if not store or not address:
        return []
    exact = store.get(address)
    if isinstance(exact, dict) and exact.get("source"):
        return [exact]

    prefix = f"{address}."
    keys = sorted((k for k in store if str(k).startswith(prefix)),
                  key=_sort_key)
    out, seen = [], set()
    for key in keys:
        citation = store.get(key)
        if not isinstance(citation, dict) or not citation.get("source"):
            continue
        identity = (citation.get("source"), citation.get("locator"),
                    citation.get("quote"))
        if identity in seen:
            continue
        seen.add(identity)
        out.append(citation)
        if len(out) >= _MAX_CITATIONS_PER_FIELD:
            break
    return out


def _source_kind(source):
    """"document" or "web", from the STORED source, not the display title.

    This read the cleaned title, which is the one string that cannot answer
    it: the cleaner strips the "Batch 3: " prefix, and the test then asked
    whether the result started with "batch". It never did, so every web
    research citation in every profile came back `type: "document"` — a file
    icon and a filename affordance over a search topic, promising a document
    the reader could open and a page they could turn to. Neither exists.
    """
    from fundos.profile.pipeline.dossier import FOUNDER_SOURCE_LABEL

    text = str(source or "").strip()
    if text == FOUNDER_SOURCE_LABEL:
        return "founder"
    if _FILENAME_SOURCE.search(text):
        return "document"
    return "web"


class ProfileFieldSourcesView(APIView):
    """GET — field-level source citations (Req 5).

    Returns an array of source objects for a given profile field, drawn
    from the latest generation run's dossier metadata. Each source
    carries a type (web, document, attribution), a title, and optionally
    a URL and snippet.

    The frontend renders these in FieldSidePanel as provenance chips.
    If no run exists or no sources can be mapped, returns an empty list
    — the frontend degrades gracefully.
    """

    def get(self, request, company_id, field_id):
        from fundos.profile.models import (
            CompanyProfile, ProfileGenerationRun, ProfileSection,
        )
        from fundos.assessment.v2_serializers import _sanitize_source_title
        from fundos.profile.spec_serializer import storage_key_for

        company = _company_or_404(request, company_id)
        profile = CompanyProfile.objects.filter(company=company).first()
        if not profile:
            # `cited` belongs in EVERY answer. Omitting it here made a
            # company with no profile the one response a caller had to
            # special-case, and a missing key reads as false only by luck.
            return Response({"field_id": field_id, "cited": False,
                             "sources": []})

        sources = []
        src_counter = 0

        # `company_profile__description_of_business` — section, then the field
        # or the item id. The separator is a DOUBLE UNDERSCORE, which is what
        # the frontend sends and what every route in this file is built from.
        #
        # This split was on a dot, so `section_key` came out empty for every
        # real request and the whole per-field branch below was unreachable.
        # Nobody noticed because the run-level fallback still returned
        # something: two lines of generalities about "web research and
        # uploaded documents", which reads like provenance and cites nothing.
        section_key, _, field_path = (field_id or "").partition("__")
        section_key = section_key or ""
        internal_key = storage_key_for(section_key) if section_key else ""

        section = None
        if internal_key:
            section = ProfileSection.objects.filter(
                profile=profile, section_key=internal_key,
                is_active=True).first()

        # 0) A document is its own source. Nothing synthesis wrote about it
        #    could be better provenance than the file, and what we made of
        #    the file is already stored beside it.
        analysis = None
        if section_key == "document_center" and field_path:
            sources, analysis = _document_analysis(profile, field_path)
            if sources:
                return Response({
                    "field_id": field_id,
                    "sectionKey": section_key,
                    "sectionName": _section_label(section_key),
                    "field": field_path,
                    "fieldName": _field_label(profile, section_key,
                                              field_path),
                    "cited": True,
                    "sources": sources,
                    "analysis": analysis,
                })

        # 1) The citations synthesis recorded for THIS field, if it has any.
        field_citations = _citations_for(
            (section.field_sources or {}) if section else None, field_path)
        for citation in field_citations:
            src_counter += 1
            # Through the same cleaner the assessment citations use, so one
            # source reads the same wherever it is shown. The model echoes
            # the dossier's own heading, which carries scaffolding a reader
            # has no use for — "Web Research (search-grounded), Batch 3:
            # Products, Services & Business Model" becomes
            # "Web Research — Products, Services & Business Model".
            raw = citation["source"]
            kind = _source_kind(raw)
            title = _sanitize_source_title(raw) or raw
            if kind == "web" and not title.lower().startswith(
                    ("web research", "company research")):
                # Say what it is. Stripped of its "Batch 3:" prefix, a topic
                # like "Products, Services & Business Model" reads as the name
                # of a document the reader can go and open, and there is no
                # such document. The cleaner already labels some of them, so
                # this fills the gap rather than saying it twice.
                title = f"Web Research · {title}"
            locator = citation.get("locator") or ""
            sources.append({
                "id": f"src_{src_counter}",
                # `name` and `locator` are the two halves a reader needs and
                # the frontend can lay out. `title` stays the joined string
                # every existing caller reads, with the locator shortened —
                # a web citation's locator is the research question, and
                # glued onto the source it made a 150-character title.
                "name": title,
                "locator": locator,
                "title": (f"{title} · {_short_locator(locator)}"
                          if locator else title),
                "type": kind,
                "snippet": citation.get("quote") or "",
                "url": "",
            })

        # 2) Failing that, whatever the section as a whole can say.
        if section and not sources:
            if section.source == "founder":
                src_counter += 1
                sources.append({
                    "id": f"src_{src_counter}",
                    "title": "Founder Entry",
                    "type": "attribution",
                    "snippet": "Entered directly by the founder.",
                    "url": "",
                })
            elif section.source in ("ai_research", "ai"):
                src_counter += 1
                sources.append({
                    "id": f"src_{src_counter}",
                    "title": "AI Research",
                    "type": "web",
                    "snippet": "Generated from web research and "
                               "uploaded documents.",
                    "url": "",
                })

        # 3) Failing even that, what the RUN can say — which is about the
        #    profile, not about this field, so it is the last resort.
        run = (ProfileGenerationRun.objects
               .filter(profile=profile, status="complete")
               .order_by("-started_at").first())
        if run and not sources:
            stats = run.source_stats or {}
            if stats.get("website", {}).get("chars"):
                src_counter += 1
                sources.append({
                    "id": f"src_{src_counter}",
                    "title": "Company Website",
                    "type": "web",
                    "snippet": f"Scraped {stats['website']['chars']:,} "
                               f"characters from the company website.",
                    "url": profile.website_url or "",
                })
            for doc_info in (stats.get("documents") or []):
                name = doc_info.get("name", "Uploaded Document")
                chars = doc_info.get("chars", 0)
                src_counter += 1
                sources.append({
                    "id": f"src_{src_counter}",
                    "title": name,
                    "type": "document",
                    "snippet": f"Extracted {chars:,} characters."
                               if chars else "Uploaded document.",
                    "url": "",
                })

        return Response({
            "field_id": field_id,
            "sectionKey": section_key,
            "field": field_path,
            # The id addresses it; these name it. An array item's address is
            # a UUID, which the UI cannot show, so it had to already know
            # what it had asked about. Not `name` — every source below
            # carries one of those, and two `name` keys a level apart is a
            # trap.
            "fieldName": _field_label(profile, section_key, field_path),
            "sectionName": _section_label(section_key),
            # A reader has to be able to tell "this value is cited" from
            # "here is what the run did in general". Without it the two are
            # rendered identically and the second passes for the first.
            "cited": bool(field_citations),
            "sources": sources,
        })


class ProfileQAView(APIView):
    """POST {question} — grounded Q&A over the retrieved profile (SIMPLE tier)."""

    def post(self, request, company_id):
        from fundos.profile.services import (answer_profile_question,
                                             get_or_create_profile)
        company = _company_or_404(request, company_id)
        profile = get_or_create_profile(company, user=request.user)
        question = (request.data or {}).get("question", "").strip()
        if not question:
            return Response({"detail": "question is required"}, status=400)
        return Response(answer_profile_question(profile, question,
                                                user=request.user))
