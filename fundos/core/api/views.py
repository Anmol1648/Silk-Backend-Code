"""
M0 API endpoints (Doc 4 — auth, contexts, masterplan, members, CKB,
notifications, admin config/alerts). Views stay thin: validate → call
service → serialise (Doc 8 §3).
"""
import hashlib
import logging
import secrets

from django.utils import timezone
from rest_framework import status
from rest_framework.permissions import AllowAny, IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fundos.core.api.base import DealScopedAPIView
from fundos.core.auth import issue_tokens
from fundos.core.exceptions import (
    AuthzError, DomainValidationError, NotFoundInDeal,
)
from fundos.core.permissions import (
    effective_roles, is_deal_owner, require_action, require_membership,
)
from fundos.core.services import ckb_service
from fundos.core.services.audit import audit
from fundos.core.services.stage_state import (
    ensure_stage_states, override_stage, recompute_stage_completion,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Auth — email OTP (Doc 6 App E: hashed, TTL, 5 attempts, single-use)
# ---------------------------------------------------------------------------

def _issue_and_send_otp(user):
    """Issue an OTP with settings-driven TTL/attempts (Doc 6 App E) and
    deliver it on the configured channel (email default; WhatsApp uses the
    AUTHENTICATION template per AppConfiguration.otp_option)."""
    from django.conf import settings as dj_settings
    from fundos.core.models import OtpToken
    from fundos.core.services.otp_delivery import send_login_otp

    ttl = int(getattr(dj_settings, "FUNDOS_OTP_TTL_SECONDS", 300))
    attempts = int(getattr(dj_settings, "FUNDOS_OTP_MAX_ATTEMPTS", 5))
    code = f"{secrets.randbelow(1000000):06d}"
    OtpToken.issue(user.email, code, ttl_seconds=ttl, max_attempts=attempts)
    send_login_otp(user, code, ttl_minutes=max(1, ttl // 60))


class OtpRequestView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        from fundos.core.throttling import OtpRequestThrottle
        return [OtpRequestThrottle()]

    def post(self, request):
        from fundos.core.models import User

        email = (request.data.get("email") or "").strip().lower()
        if not email or "@" not in email:
            raise DomainValidationError("A valid email is required.",
                                        fields={"email": "invalid"})
        user = User.objects.filter(email=email, is_deleted=False).first()
        if user:
            _issue_and_send_otp(user)
            return Response({
                "is_registered": True,
                "user_exists": True,
                "detail": "OTP sent successfully."
            })
        else:
            from django.conf import settings as dj_settings
            if getattr(dj_settings, "FUNDOS_OTP_ECHO_CONSOLE", False):
                logger.info("OTP: no live account for %s — nothing sent.",
                            email)
            return Response({
                "is_registered": False,
                "user_exists": False,
                "detail": "Account does not exist. Please complete signup first."
            })


class SignupView(APIView):
    """POST /auth/signup {email, name?, companyName?} — Gap G2 JIT
    onboarding: creates tenant + user (+ company & owner membership) and
    sends the login OTP."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        from fundos.core.throttling import OtpRequestThrottle
        return [OtpRequestThrottle()]

    def post(self, request):
        from fundos.core.services import onboarding
        user, created = onboarding.signup(
            request.data.get("email"),
            name=request.data.get("name") or "",
            company_name=request.data.get("companyName") or "")
        _issue_and_send_otp(user)
        return Response({
            "is_registered": True,
            "user_exists": True,
            "created": created,
            "detail": "Verification code sent. Verify the OTP to complete sign-in."
        }, status=status.HTTP_202_ACCEPTED)


class OtpVerifyView(APIView):
    authentication_classes = []
    permission_classes = [AllowAny]

    def get_throttles(self):
        from fundos.core.throttling import OtpVerifyThrottle
        return [OtpVerifyThrottle()]

    def post(self, request):
        from fundos.core.models import OtpToken, User

        email = (request.data.get("email") or "").strip().lower()
        # Gap G1: the contract standardises on 'code' but 'otp' is accepted
        # as an alias; a MISSING field is a 422 validation error, distinct
        # from a WRONG code (403), so frontends can debug the difference.
        raw = request.data.get("code", request.data.get("otp"))
        if raw in (None, ""):
            raise DomainValidationError(
                "The one-time code is required (field 'code'; 'otp' is "
                "accepted as an alias).", fields={"code": "required"})
        code = str(raw).strip()
        if not email:
            raise DomainValidationError("Email is required.",
                                        fields={"email": "required"})

        user = User.objects.filter(email=email, is_deleted=False,
                                   is_active=True).first()
        outcome = OtpToken.verify(email, code) if user else "incorrect"
        if outcome != "ok":
            # Generic message — never reveal account existence.
            raise AuthzError("Invalid or expired code.")

        access, refresh = issue_tokens(user)
        audit("auth.login", actor=user, entity="user", entity_id=user.id,
              tenant_id=user.tenant_id)
        return Response({"accessToken": access, "refreshToken": refresh,
                         "user": {"id": str(user.id), "email": user.email,
                                  "displayName": user.name,
                                  "isInternal": user.is_internal}})


class TokenRefreshView(APIView):
    """POST /auth/refresh {refreshToken} — refresh tokens were issued but
    unusable before (no endpoint existed): functional gap found in review."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def post(self, request):
        import jwt as pyjwt

        from fundos.core.auth import decode_token
        from fundos.core.models import User

        token = request.data.get("refreshToken") or ""
        if not token:
            raise DomainValidationError("refreshToken is required.",
                                        fields={"refreshToken": "required"})
        try:
            payload = decode_token(token, expected_type="refresh")
        except pyjwt.PyJWTError:
            raise AuthzError("Invalid or expired refresh token.")
        user = User.objects.filter(id=payload.get("sub"), is_deleted=False,
                                   is_active=True).first()
        if not user:
            raise AuthzError("Invalid or expired refresh token.")
        access, refresh = issue_tokens(user)
        return Response({"accessToken": access, "refreshToken": refresh})


_CONTEXT_SORT_FIELDS = {
    # query value -> internal item key used for comparison
    "createdat": "_createdAt",
    "companyname": "companyName",
    "sector": "_sector",
    "name": "companyName",
    "date": "_createdAt",
}
_INTERNAL_CONTEXT_KEYS = ("_createdAt", "_sector", "_lastRaiseDate")


def _iso_or_none(value):
    """ISO-8601 string, or None. Used for the timestamps the dashboard reads."""
    return value.isoformat() if value else None


def _sort_contexts(items, sort_by, order):
    """Sort /me/contexts items. Default: newest first (createdAt desc).

    Sorting is stable and null-safe (rows missing the sort value always sort
    to the END, regardless of direction); internal sort-only keys are stripped
    from the returned items so the response shape is unchanged.
    """
    field_key = _CONTEXT_SORT_FIELDS.get((sort_by or "").strip().lower(),
                                         "_createdAt")
    descending = (order or "desc").strip().lower() != "asc"

    def comparable(item):
        value = item.get(field_key)
        if field_key == "_createdAt":
            present = value is not None
            return present, (value.isoformat() if present else "")
        text = (str(value) if value is not None else "").strip().lower()
        return (text != ""), text

    # Sort the "present" rows by their value in the requested direction, and
    # always append the "missing" rows afterwards.
    present = [it for it in items if comparable(it)[0]]
    missing = [it for it in items if not comparable(it)[0]]
    present.sort(key=lambda it: comparable(it)[1], reverse=descending)
    ordered = present + missing

    for it in ordered:
        for k in _INTERNAL_CONTEXT_KEYS:
            it.pop(k, None)
    return ordered


class MeContextsView(APIView):
    """GET /me/contexts — companies/deals the caller can enter (Doc 4 M0).

    Default order is NEWEST FIRST (by creation date, descending) — enforced
    explicitly, not left to insertion order. Clients may override with query
    params (issue-financial-summary-and-sort-order.md, Issue 2):

        ?sortBy=createdAt|companyName|sector   (default: createdAt)
        ?order=desc|asc                         (default: desc)

    Values are case-insensitive. Unknown sortBy/order fall back to the
    defaults rather than erroring, so a bad param never breaks the listing.
    """
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fundos.core.models import Company, Deal, Membership

        # Issue 3: memberships can point at deals in OTHER tenants
        # (cross-tenant invites), so the context listing must not be
        # filtered by the caller's home tenant.
        memberships = Membership.all_objects.filter(user=request.user,
                                                    status="active",
                                                    is_deleted=False)
        deal_ids = [m.scope_id for m in memberships
                    if m.scope_type == "deal"]
        company_ids = [m.scope_id for m in memberships
                       if m.scope_type == "company"]
        deals = {d.id: d for d in
                 Deal.all_objects.filter(id__in=deal_ids, is_deleted=False)
                                 .select_related("company")}
        companies = {c.id: c for c in Company.all_objects.filter(
            id__in=company_ids, is_deleted=False)}

        # Per-company summary fields for the All Companies view (Gap doc
        # §3.2 / §10.1): sector, lastRaise, totalFundingReceivedUsdMn,
        # attachmentLinks. Computed in bulk for every company the caller can
        # see, then attached to scope:"company" items only — deal items are
        # unchanged. Defensive: if enrichment fails for any reason the
        # contexts listing still returns, just without the extra fields.
        company_summary = {}
        try:
            from fundos.profile.summary import company_summaries
            summary_ids = list(companies.keys()) + [
                d.company_id for d in deals.values()]
            company_summary = company_summaries(summary_ids)
        except Exception:
            company_summary = {}

        contexts = []
        for m in memberships:
            if m.scope_type == "deal" and m.scope_id in deals:
                deal = deals[m.scope_id]
                contexts.append({
                    "scope": "deal", "dealId": str(deal.id),
                    "dealName": deal.name,
                    "companyId": str(deal.company_id),
                    "companyName": deal.company.name,
                    "role": m.role,
                    # The dashboard sorts on updatedAt and renders it as the
                    # "last touched" chip; it was never sent, so the sort was
                    # a no-op and the chip never appeared.
                    "updatedAt": _iso_or_none(deal.updated_at),
                    "createdAt": _iso_or_none(deal.created_at),
                    "_createdAt": deal.created_at,
                    "_sector": "", "_lastRaiseDate": None})
            elif m.scope_type == "company" and m.scope_id in companies:
                company = companies[m.scope_id]
                item = {
                    "scope": "company", "companyId": str(company.id),
                    "companyName": company.name, "role": m.role,
                    "updatedAt": _iso_or_none(company.updated_at),
                    "createdAt": _iso_or_none(company.created_at)}
                summary = company_summary.get(str(company.id), {})
                item.update(summary)
                item["_createdAt"] = company.created_at
                item["_sector"] = summary.get("sector", "") or ""
                item["_lastRaiseDate"] = (summary.get("lastRaise") or {}).get(
                    "date") or ""
                contexts.append(item)
        # Deals directly owned (primary_owner) even without a membership row.
        for deal in Deal.all_objects.filter(primary_owner=request.user,
                                            is_deleted=False) \
                                    .select_related("company"):
            if str(deal.id) not in [c.get("dealId") for c in contexts]:
                contexts.append({
                    "scope": "deal", "dealId": str(deal.id),
                    "dealName": deal.name,
                    "companyId": str(deal.company_id),
                    "companyName": deal.company.name, "role": "founder",
                    "updatedAt": _iso_or_none(deal.updated_at),
                    "createdAt": _iso_or_none(deal.created_at),
                    "_createdAt": deal.created_at,
                    "_sector": "", "_lastRaiseDate": None})

        contexts = _sort_contexts(contexts,
                                  request.query_params.get("sortBy"),
                                  request.query_params.get("order"))

        return Response({"items": contexts,
                         "lastActiveDealId": str(request.user.last_active_deal_id)
                         if request.user.last_active_deal_id else None})


class ContextSwitchView(APIView):
    """POST /contexts/switch {dealId} — Gap G3. Records the user's active
    deal for landing-page routing and notification context. Pure UX — deal
    authority always remains the per-URL membership check."""

    permission_classes = [IsAuthenticated]

    def post(self, request):
        deal_id = request.data.get("dealId")
        # Clients that lost the deal id can send the literal strings
        # "undefined"/"null"; treat them as a missing value (422), never as a
        # lookup that would explode into a 500.
        if isinstance(deal_id, str) and deal_id.strip().lower() in (
                "", "undefined", "null", "none"):
            deal_id = None
        if not deal_id:
            raise DomainValidationError("dealId is required.",
                                        fields={"dealId": "required"})
        deal = require_membership(request.user, deal_id)   # 403 if not member
        request.user.last_active_deal_id = deal.id
        request.user.save(update_fields=["last_active_deal_id"])
        audit("context.switched", actor=request.user, deal_id=deal.id,
              entity="deal", entity_id=deal.id)
        return Response({"activeDealId": str(deal.id),
                         "dealName": deal.name,
                         "companyId": str(deal.company_id)})


# ---------------------------------------------------------------------------
# Onboarding — companies & deals (Gap G2)
# ---------------------------------------------------------------------------

def _serialise_company(c):
    return {"id": str(c.id), "name": c.name, "domain": c.domain or None,
            "hqCountry": c.hq_country or None,
            "createdAt": c.created_at.isoformat()}


class CompaniesView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fundos.core.services import onboarding
        return Response({"items": [
            _serialise_company(c)
            for c in onboarding.list_companies(request.user)]})

    def post(self, request):
        from fundos.core.services import onboarding
        company = onboarding.create_company(
            request.user, name=request.data.get("name"),
            domain=request.data.get("domain") or "",
            hq_country=request.data.get("hqCountry") or "")
        return Response(_serialise_company(company),
                        status=status.HTTP_201_CREATED)


class CompanyDetailView(APIView):
    permission_classes = [IsAuthenticated]

    def _get_company(self, request, company_id):
        from fundos.core.models import Company
        company = Company.objects.filter(id=company_id,
                                         tenant_id=request.user.tenant_id).first()
        if not company:
            raise AuthzError()          # never reveal existence
        return company

    def get(self, request, company_id):
        return Response(_serialise_company(
            self._get_company(request, company_id)))

    def patch(self, request, company_id):
        from fundos.core.services import onboarding
        company = onboarding.update_company(
            request.user, self._get_company(request, company_id),
            **{k: request.data.get(k) for k in ("name", "domain", "hqCountry")
               if k in request.data})
        return Response(_serialise_company(company))

    def delete(self, request, company_id):
        """DELETE /companies/{id} — permanently remove the company workspace
        and all associated data (deals, profile, documents + physical files,
        memberships/contexts). Owner-gated; 204 on success (LLM_ISSUE3)."""
        from fundos.core.services import onboarding
        company = self._get_company(request, company_id)
        onboarding.delete_company(request.user, company)
        return Response(status=status.HTTP_204_NO_CONTENT)


class CompanyLogoView(APIView):
    """PATCH /companies/{id}/logo — set the company logo.

    The client (LogoUploader) has always called this endpoint, but no route
    or handler existed, so every logo change returned 404 and the picture
    silently reverted on the next load. The logo could only ever be set as a
    side effect of onboarding.

    Accepts either form the onboarding payload accepts:
      {"logoUrl": "https://..."}   — stored verbatim
      {"logoBase64": "data:..."}   — persisted to storage, URL stored
    """

    permission_classes = [IsAuthenticated]

    def patch(self, request, company_id):
        from fundos.core.models import Company
        from fundos.profile.logo import resolve_logo
        from fundos.profile.services import get_or_create_profile

        company = Company.objects.filter(
            id=company_id, tenant_id=request.user.tenant_id).first()
        if not company:
            raise AuthzError()          # never reveal existence

        profile = get_or_create_profile(company, user=request.user)
        logo = resolve_logo(profile,
                            logo_url=request.data.get("logoUrl") or "",
                            logo_base64=request.data.get("logoBase64") or "")
        if not logo:
            raise DomainValidationError(
                "Provide a logo URL or an image to upload.",
                fields={"logoUrl": "required"})

        profile.logo_url = logo
        profile.save(update_fields=["logo_url", "updated_at"])
        audit("company.logo_updated", actor=request.user,
              entity="company_profile", entity_id=profile.id,
              meta={"companyId": str(company.id)})
        return Response({"companyId": str(company.id), "logoUrl": logo})

    def delete(self, request, company_id):
        """Clear the logo and fall back to the initial-letter avatar."""
        from fundos.core.models import Company
        from fundos.profile.services import get_or_create_profile

        company = Company.objects.filter(
            id=company_id, tenant_id=request.user.tenant_id).first()
        if not company:
            raise AuthzError()
        profile = get_or_create_profile(company, user=request.user)
        profile.logo_url = ""
        profile.save(update_fields=["logo_url", "updated_at"])
        return Response({"companyId": str(company.id), "logoUrl": None})


class CompanyDealsView(APIView):
    """POST /companies/{id}/deals — create a fundraising round (owner-gated);
    provisions membership + stage states + CKB so the deal is immediately
    usable. GET lists the company's deals visible to the caller."""

    permission_classes = [IsAuthenticated]

    def get(self, request, company_id):
        from fundos.core.models import Deal, Membership
        member_deal_ids = set(Membership.objects.filter(
            user=request.user, scope_type="deal",
            status="active").values_list("scope_id", flat=True))
        deals = Deal.objects.filter(company_id=company_id,
                                    tenant_id=request.user.tenant_id)
        return Response({"items": [{
            "id": str(d.id), "name": d.name, "roundType": d.round_type,
            "status": d.status,
        } for d in deals
            if d.id in member_deal_ids
            or str(d.primary_owner_id) == str(request.user.id)]})

    def post(self, request, company_id):
        from fundos.core.models import Company
        from fundos.core.services import onboarding
        company = Company.objects.filter(
            id=company_id, tenant_id=request.user.tenant_id).first()
        if not company:
            raise AuthzError()
        deal = onboarding.create_deal(
            request.user, company, name=request.data.get("name"),
            round_type=request.data.get("roundType") or "")
        # Emit BOTH keys: "dealId" (context/switch convention) and "id"
        # (the convention used by the GET list and by POST /companies).
        # Clients reading either key get a usable identifier.
        return Response({"dealId": str(deal.id), "id": str(deal.id),
                         "name": deal.name,
                         "roundType": deal.round_type},
                        status=status.HTTP_201_CREATED)


# ---------------------------------------------------------------------------
# Masterplan & stage states
# ---------------------------------------------------------------------------

class MasterplanView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.core.models import ArtifactStatus, StageState

        ensure_stage_states(self.deal)
        recompute_stage_completion(self.deal.id)
        stages = [{
            "stageNo": s.stage_no, "status": s.status,
            "completionPct": float(s.completion_pct),
            "hardGateMet": s.hard_gate_met,
            "unlockedByOverride": s.unlocked_by_override,
        } for s in StageState.objects.filter(deal_id=self.deal.id)
                                     .order_by("stage_no")]
        pending = [{
            "artifactType": a.artifact_type, "status": a.status,
            "reason": a.reason,
        } for a in ArtifactStatus.objects.filter(deal_id=self.deal.id)
                                         .exclude(status="current")]
        return Response({"dealId": str(self.deal.id), "stages": stages,
                         "pendingArtifacts": pending})


class StageOverrideView(DealScopedAPIView):
    required_action = "stage.override"

    def post(self, request, deal_id, stage_no):
        state = override_stage(self.deal, int(stage_no), request.user)
        return Response({"stageNo": state.stage_no, "status": state.status,
                         "unlockedByOverride": True})


# ---------------------------------------------------------------------------
# Members
# ---------------------------------------------------------------------------

class MembersView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.core.models import Membership
        items = [{
            "userId": str(m.user_id), "email": m.user.email,
            "displayName": m.user.name, "role": m.role,
            "status": m.status,
        } for m in Membership.objects.filter(scope_type="deal",
                                             scope_id=self.deal.id)
                                     .select_related("user")]
        return Response({"items": items})

    def post(self, request, deal_id):
        """Invite/add a member (owner-only: members.manage)."""
        require_action(request.user, self.deal, "members.manage")
        from fundos.core.models import Membership, User

        email = (request.data.get("email") or "").strip().lower()
        role = request.data.get("role") or "team"
        if not email:
            raise DomainValidationError("Email is required.",
                                        fields={"email": "required"})
        user = User.objects.filter(email=email, is_deleted=False).first()
        if not user:
            user = User.objects.create_user(
                email=email, tenant_id=self.deal.tenant_id,
                name=email.split("@")[0])
        membership, created = Membership.all_objects.get_or_create(
            tenant_id=self.deal.tenant_id, user=user, scope_type="deal",
            scope_id=self.deal.id, role=role, is_deleted=False,
            defaults={"status": "active", "invited_by": request.user})
        audit("member.invited", actor=request.user, deal_id=self.deal.id,
              entity="membership", entity_id=membership.id,
              meta={"email": email, "role": role})
        # Tester Issue 4: member management produced NO in-app notification —
        # emit_alert only fires alerts an admin has subscribed to. Member
        # events now write Notification rows directly: one to the invited
        # user, one to the deal owner (skipped when they're the same person).
        from fundos.core.models import Notification
        Notification.objects.create(
            tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
            recipient=user, type="member.invited",
            title=f"You've been added to {self.deal.name}",
            body=(f"{request.user.name or request.user.email} added you to "
                  f"{self.deal.company.name} — {self.deal.name} as {role}."),
            payload={"dealId": str(self.deal.id), "role": role})
        if request.user.id != user.id:
            Notification.objects.create(
                tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
                recipient=request.user, type="member.invited",
                title=f"{email} added to {self.deal.name}",
                body=f"{email} now has {role} access to {self.deal.name}.",
                payload={"dealId": str(self.deal.id), "email": email,
                         "role": role})
        from fundos.core.alerting.tasks import emit_alert
        emit_alert("fundos.member.invited", {
            "deal_id": str(self.deal.id), "email": email, "role": role,
            "company": self.deal.company.name,
            "tenant_id": str(self.deal.tenant_id)})
        return Response({"userId": str(user.id), "role": membership.role},
                        status=status.HTTP_201_CREATED)


class MemberDetailView(DealScopedAPIView):
    def delete(self, request, deal_id, user_id):
        require_action(request.user, self.deal, "members.manage")
        from fundos.core.models import Membership
        membership = Membership.objects.filter(
            scope_type="deal", scope_id=self.deal.id,
            user_id=user_id).first()
        if not membership:
            raise NotFoundInDeal("Member not found.")
        if str(self.deal.primary_owner_id) == str(user_id):
            raise DomainValidationError(
                "The deal owner cannot be removed.")
        membership.delete()
        audit("member.removed", actor=request.user, deal_id=self.deal.id,
              entity="membership", meta={"userId": str(user_id)})
        # Tester Issue 4: notify both sides of the removal.
        from fundos.core.models import Notification
        removed_user = membership.user
        Notification.objects.create(
            tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
            recipient=removed_user, type="member.removed",
            title=f"Your access to {self.deal.name} was removed",
            body=f"You no longer have access to {self.deal.company.name} — "
                 f"{self.deal.name}.",
            payload={"dealId": str(self.deal.id)})
        if request.user.id != removed_user.id:
            Notification.objects.create(
                tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
                recipient=request.user, type="member.removed",
                title=f"{removed_user.email} removed from {self.deal.name}",
                body=f"{removed_user.email} no longer has access to "
                     f"{self.deal.name}.",
                payload={"dealId": str(self.deal.id),
                         "email": removed_user.email})
        return Response(status=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# CKB
# ---------------------------------------------------------------------------

def _serialise_field(f):
    out = {
        "fieldKey": f.field_key, "groupKey": f.group_key, "value": f.value,
        "ccy": f.ccy or None, "basis": f.basis or None, "source": f.source,
        "confidence": float(f.confidence) if f.confidence is not None else None,
        "verified": f.verified,
        "factOrInference": f.fact_or_inference or None,
        "sourceRef": f.source_ref or None,
        "suggestedValue": f.suggested_value_json or None,
        "updatedAt": f.updated_at.isoformat(),
    }
    # TC-021: money fields carry an explicit USD equivalent so the UI can
    # show both without re-deriving the rate.
    try:
        if (f.ccy or "").upper() == "INR" and f.value is not None:
            from fundos.core.services.currency import usd_inr_rate
            out["usdValue"] = round(float(f.value) / usd_inr_rate(), 2)
            out["fxUsdInr"] = usd_inr_rate()
    except (TypeError, ValueError):
        pass
    return out


class CkbView(DealScopedAPIView):
    def get(self, request, deal_id):
        """Tester Issues 5/6/16: a fresh deal returned `groups: {}` because
        CkbField rows are only created on first write — so the UI had no
        structure to render and founders could never enter ARR/MRR/burn/etc,
        which blocked valuation and everything downstream. The GET now
        serves the FULL field catalogue: persisted rows where they exist,
        empty editable placeholders everywhere else."""
        from fundos.core.models import CkbField
        from fundos.core.services.ckb_service import (
            FIELD_GROUPS, MONETARY_FIELDS,
        )
        ckb = ckb_service.get_or_create_ckb(self.deal)

        persisted = {f.field_key: f
                     for f in CkbField.objects.filter(deal_id=self.deal.id)}

        GROUP_ORDER = ["company", "business", "financial", "customers",
                       "team", "shareholding", "legal"]
        groups = {g: [] for g in GROUP_ORDER}
        for field_key, group_key in FIELD_GROUPS.items():
            f = persisted.pop(field_key, None)
            if f is not None:
                groups.setdefault(group_key, []).append(_serialise_field(f))
            else:
                groups.setdefault(group_key, []).append({
                    "fieldKey": field_key, "groupKey": group_key,
                    "value": None,
                    "ccy": "INR" if field_key in MONETARY_FIELDS else None,
                    "basis": None, "source": None, "confidence": None,
                    "verified": False, "factOrInference": None,
                    "sourceRef": None, "suggestedValue": None,
                    "updatedAt": None,
                })
        # Any persisted keys outside the catalogue (research extensions)
        # still surface under their stored group.
        for f in persisted.values():
            groups.setdefault(f.group_key, []).append(_serialise_field(f))
        groups = {g: fields for g, fields in groups.items() if fields}

        # Tester Issue 1: section assignments existed server-side but were
        # never returned, so the UI could not display (or offer) them.
        from fundos.core.models import CkbSectionAssignment
        assignments = {}
        for a in CkbSectionAssignment.objects.filter(
                deal_id=self.deal.id).select_related("assignee"):
            assignments[a.group_key] = {
                "assigneeUserId": str(a.assignee_id),
                "assigneeEmail": a.assignee.email,
                "assigneeName": a.assignee.name or a.assignee.email,
                "status": a.status,
            }

        return Response({
            "completenessPct": float(ckb.completeness_pct),
            "groups": groups,
            "assignments": assignments,
            "warnings": ckb_service.consistency_warnings(self.deal)})

    def patch(self, request, deal_id):
        """PATCH {fields: [{fieldKey, value, ccy?, basis?}]} — founder edits
        verify + propagate (BR-M0-012)."""
        require_action(request.user, self.deal, "ckb.edit")
        results, warnings = [], []
        for item in request.data.get("fields", []):
            field, warns = ckb_service.set_field(
                self.deal, item.get("fieldKey"), item.get("value"),
                user=request.user, source="founder",
                ccy=item.get("ccy", ""), basis=item.get("basis", ""),
                reason=item.get("reason", ""))
            results.append(_serialise_field(field))
            warnings = warns
        return Response({"fields": results, "warnings": warnings})


class CkbSuggestionActionView(DealScopedAPIView):
    """Accept/reject a parked AI suggestion (BR-M0-011)."""
    required_action = "ckb.edit"

    def post(self, request, deal_id, field_key):
        from fundos.core.models import CkbField
        field = CkbField.objects.filter(deal_id=self.deal.id,
                                        field_key=field_key).first()
        if not field:
            raise NotFoundInDeal("No such field.")
        action = request.data.get("action")
        if action not in ("accept", "reject"):
            raise DomainValidationError("action must be accept|reject.",
                                        fields={"action": "invalid"})

        # Case 1 — a parked suggestion beside a verified value (BR-M0-011).
        if field.suggested_value_json:
            if action == "accept":
                suggestion = field.suggested_value_json
                # Un-park first — set_field re-parks AI writes against a
                # verified field, which is exactly what accept overrides.
                field.suggested_value_json = None
                field.verified = False
                field.save(update_fields=["suggested_value_json",
                                          "verified", "updated_at"])
                field, warnings = ckb_service.set_field(
                    self.deal, field_key, suggestion.get("value"),
                    user=request.user,
                    source=suggestion.get("source") or "ai_research",
                    confidence=suggestion.get("confidence"),
                    source_ref=suggestion.get("source_ref", ""),
                    verify=True, reason="accepted AI suggestion")
                return Response({"field": _serialise_field(field),
                                 "warnings": warnings})
            field.suggested_value_json = None
            field.save(update_fields=["suggested_value_json", "updated_at"])
            audit("ckb.suggestion.rejected", actor=request.user,
                  deal_id=self.deal.id, entity="ckb_field",
                  entity_id=field.id, meta={"fieldKey": field_key})
            return Response({"field": _serialise_field(field)})

        # Case 2 — an unverified AI-discovered value with no parked
        # suggestion (QA BUG-004): the founder must be able to confirm it
        # WITHOUT editing, which would reassign the source to "founder"
        # and permanently discard the research provenance + confidence.
        if not field.verified and field.source in ("ai_research",
                                                   "document_extracted"):
            if action == "accept":
                field.verified = True
                field.save(update_fields=["verified", "updated_at"])
                audit("ckb.ai_value.accepted", actor=request.user,
                      deal_id=self.deal.id, entity="ckb_field",
                      entity_id=field.id,
                      meta={"fieldKey": field_key,
                            "provenanceRetained": field.source})
                return Response({"field": _serialise_field(field),
                                 "warnings": []})
            # reject → clear the proposed value; it resurfaces as a gap.
            field, warnings = ckb_service.set_field(
                self.deal, field_key, None, user=request.user,
                source="founder", reason="rejected AI-discovered value")
            field.verified = False
            field.save(update_fields=["verified", "updated_at"])
            audit("ckb.ai_value.rejected", actor=request.user,
                  deal_id=self.deal.id, entity="ckb_field",
                  entity_id=field.id, meta={"fieldKey": field_key})
            return Response({"field": _serialise_field(field),
                             "warnings": warnings})

        raise NotFoundInDeal("No suggestion parked for this field.")


class CkbSectionAssignView(DealScopedAPIView):
    """Delegate CKB sections to team members (Doc 1 M0-§2 §7.5)."""
    required_action = "ckb.assign"

    def post(self, request, deal_id):
        from fundos.core.models import (
            CkbSectionAssignment, Notification, User,
        )
        group_key = request.data.get("groupKey")
        assignee_id = request.data.get("assigneeUserId")
        if not group_key:
            raise DomainValidationError("groupKey required.",
                                        fields={"groupKey": "required"})
        # Unassign: null assignee clears the assignment (Issue 1).
        if not assignee_id:
            CkbSectionAssignment.objects.filter(
                deal_id=self.deal.id, group_key=group_key).delete()
            audit("ckb.section.unassigned", actor=request.user,
                  deal_id=self.deal.id, entity="ckb_section_assignment",
                  meta={"groupKey": group_key})
            return Response({"groupKey": group_key,
                             "assigneeUserId": None})

        # User's manager is identity-based (not tenant-scoped), so a
        # cross-tenant invited member resolves correctly here.
        assignee = User.objects.filter(id=assignee_id,
                                       is_deleted=False).first()
        if not assignee:
            raise DomainValidationError("assigneeUserId not found.",
                                        fields={"assigneeUserId": "invalid"})
        require_membership(assignee, self.deal.id)   # must be a member
        assignment, _ = CkbSectionAssignment.objects.update_or_create(
            deal_id=self.deal.id, group_key=group_key,
            defaults={"tenant_id": self.deal.tenant_id,
                      "assignee": assignee,
                      "assigned_by": request.user, "status": "assigned"})
        audit("ckb.section.assigned", actor=request.user,
              deal_id=self.deal.id, entity="ckb_section_assignment",
              entity_id=assignment.id,
              meta={"groupKey": group_key, "assignee": str(assignee.id)})
        # Issue 1 expected result: the assignee learns about it.
        if assignee.id != request.user.id:
            Notification.objects.create(
                tenant_id=self.deal.tenant_id, deal_id=self.deal.id,
                recipient=assignee, type="ckb.section.assigned",
                title=f"Knowledge Base section assigned to you",
                body=(f"{request.user.name or request.user.email} assigned "
                      f"the '{group_key.title()}' section of "
                      f"{self.deal.name} to you."),
                payload={"dealId": str(self.deal.id),
                         "groupKey": group_key})
        return Response({"groupKey": group_key,
                         "assigneeUserId": str(assignee.id),
                         "assigneeEmail": assignee.email,
                         "assigneeName": assignee.name or assignee.email})


# ---------------------------------------------------------------------------
# Notifications (in-app)
# ---------------------------------------------------------------------------

class NotificationsView(APIView):
    permission_classes = [IsAuthenticated]

    def get(self, request):
        from fundos.core.models import Notification
        items = [{
            "id": str(n.id), "type": n.type, "title": n.title,
            "body": n.body, "payload": n.payload,
            "dealId": str(n.deal_id) if n.deal_id else None,
            "isRead": n.is_read,
            "createdAt": n.created_at.isoformat(),
        } for n in Notification.objects.filter(recipient=request.user)
                                       .order_by("-created_at")[:50]]
        return Response({"items": items})


class NotificationReadView(APIView):
    permission_classes = [IsAuthenticated]

    def post(self, request, notification_id):
        from fundos.core.models import Notification
        n = Notification.objects.filter(id=notification_id,
                                        recipient=request.user).first()
        if not n:
            raise NotFoundInDeal("Notification not found.")
        n.is_read = True
        n.save(update_fields=["is_read"])
        return Response({"id": str(n.id), "isRead": True})


# ---------------------------------------------------------------------------
# Dashboard & stage-state (Doc 4 M0 — missing surfaces found in the
# cross-functionality sweep)
# ---------------------------------------------------------------------------

class StageStateView(DealScopedAPIView):
    """GET /deals/{id}/stage-state (Doc 4 M0)."""

    def get(self, request, deal_id):
        from fundos.core.models import StageState
        ensure_stage_states(self.deal)
        recompute_stage_completion(self.deal.id)
        return Response({"dealId": str(self.deal.id), "stages": [{
            "stageNo": s.stage_no, "status": s.status,
            "completionPct": float(s.completion_pct),
            "hardGateMet": s.hard_gate_met,
            "unlockedByOverride": s.unlocked_by_override,
        } for s in StageState.objects.filter(deal_id=self.deal.id)
                                     .order_by("stage_no")]})


class DashboardView(DealScopedAPIView):
    """GET /deals/{id}/dashboard (Doc 4 M0) — a single landing summary."""

    def get(self, request, deal_id):
        from fundos.core.models import (
            ArtifactStatus, GenerationJob, Notification, StageState,
        )
        from fundos.core.services import ckb_service

        ensure_stage_states(self.deal)
        recompute_stage_completion(self.deal.id)
        ckb = ckb_service.get_or_create_ckb(self.deal)
        stages = list(StageState.objects.filter(deal_id=self.deal.id)
                      .order_by("stage_no"))
        current = next((s for s in stages
                        if s.status in ("available", "in_progress")), None)
        pending = ArtifactStatus.objects.filter(
            deal_id=self.deal.id).exclude(status="current")
        jobs_qs = GenerationJob.objects.filter(
            deal_id=self.deal.id).order_by("-created_at")[:5]
        from fundos.core.services import jobs as jobs_service
        return Response({
            "dealId": str(self.deal.id), "dealName": self.deal.name,
            "companyName": self.deal.company.name,
            "roundType": self.deal.round_type,
            "currentStage": current.stage_no if current else None,
            "stages": [{"stageNo": s.stage_no, "status": s.status,
                        "completionPct": float(s.completion_pct)}
                       for s in stages],
            "ckbCompletenessPct": float(ckb.completeness_pct),
            "pendingArtifacts": [{"artifactType": a.artifact_type,
                                  "status": a.status, "reason": a.reason}
                                 for a in pending],
            "unreadNotifications": Notification.objects.filter(
                recipient=request.user, is_read=False).count(),
            "recentJobs": [jobs_service.serialise(j) for j in jobs_qs],
        })


# ---------------------------------------------------------------------------
# CKB per-field PATCH (Doc 4 contract: PATCH /ckb/fields/{fieldKey})
# ---------------------------------------------------------------------------

class CkbFieldView(DealScopedAPIView):
    """PATCH /deals/{id}/ckb/fields/{fieldKey} — the per-field verb the API
    design documents; also carries accept/reject of parked AI suggestions
    via {"action": "accept"|"reject"} for design parity."""

    required_action = "ckb.edit"

    def patch(self, request, deal_id, field_key):
        action = request.data.get("action")
        if action in ("accept", "reject"):
            proxy = CkbSuggestionActionView()
            proxy.deal = self.deal
            return proxy.post(request, deal_id, field_key)
        field, warnings = ckb_service.set_field(
            self.deal, field_key, request.data.get("value"),
            user=request.user, source="founder",
            ccy=request.data.get("ccy", ""),
            basis=request.data.get("basis", ""),
            reason=request.data.get("reason", ""))
        return Response({"field": _serialise_field(field),
                         "warnings": warnings})


# ---------------------------------------------------------------------------
# Generation jobs (Gap G6)
# ---------------------------------------------------------------------------

class JobsView(DealScopedAPIView):
    def get(self, request, deal_id):
        from fundos.core.models import GenerationJob
        from fundos.core.services import jobs as jobs_service
        qs = GenerationJob.objects.filter(deal_id=self.deal.id)
        kind = request.query_params.get("kind")
        if kind:
            qs = qs.filter(kind=kind)
        return Response({"items": [jobs_service.serialise(j)
                                   for j in qs.order_by("-created_at")[:20]]})


class JobDetailView(DealScopedAPIView):
    def get(self, request, deal_id, job_id):
        from fundos.core.models import GenerationJob
        from fundos.core.services import jobs as jobs_service
        job = GenerationJob.objects.filter(id=job_id,
                                           deal_id=self.deal.id).first()
        if not job:
            raise NotFoundInDeal("Job not found.")
        return Response(jobs_service.serialise(job))


# ---------------------------------------------------------------------------
# Signed file downloads (local backend). Gap found in review: the local
# storage connector already emits /api/v1/files/{path}?expires&sig URLs,
# but no route existed to serve them — signed downloads were broken.
# ---------------------------------------------------------------------------

class SignedFileView(APIView):
    """GET /files/{path}?expires&sig — HMAC-validated download (dev/local).
    The signature is the authorisation; S3/OCI backends presign natively."""

    authentication_classes = []
    permission_classes = [AllowAny]

    def get(self, request, remote_path):
        import hmac as hmac_mod
        import os
        import time

        from django.conf import settings as dj_settings
        from django.http import FileResponse

        expires = request.query_params.get("expires") or "0"
        sig = request.query_params.get("sig") or ""
        try:
            expires_i = int(expires)
        except ValueError:
            raise AuthzError("Invalid signature.")
        if expires_i < int(time.time()):
            raise AuthzError("Link expired.")
        expected = hmac_mod.new(
            dj_settings.SECRET_KEY.encode(),
            f"{remote_path}:{expires_i}".encode(), "sha256").hexdigest()[:32]
        if not hmac_mod.compare_digest(sig, expected):
            raise AuthzError("Invalid signature.")

        root = getattr(dj_settings, "FUNDOS_STORAGE_LOCAL_ROOT", "")
        path = os.path.normpath(os.path.join(root, remote_path.lstrip("/")))
        if not root or not path.startswith(os.path.normpath(root)) \
                or not os.path.isfile(path):
            raise NotFoundInDeal("File not found.")
        return FileResponse(open(path, "rb"), as_attachment=True,
                            filename=os.path.basename(path))
