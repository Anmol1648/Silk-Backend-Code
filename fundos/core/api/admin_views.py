"""
Admin/config API (Doc 4 M0 admin surface): AppConfiguration, alert
definitions CRUD + evaluate-now (the Gyain admin surface, Doc 6 §6.6),
LLM registry & bindings, scoring-config versions, master values.

All endpoints require the fundos_admin role (platform staff).
"""
import logging

from rest_framework import status
from rest_framework.permissions import IsAuthenticated
from rest_framework.response import Response
from rest_framework.views import APIView

from fundos.core.exceptions import AuthzError, DomainValidationError

logger = logging.getLogger(__name__)


def require_platform_admin(user):
    from fundos.core.models import Membership
    is_admin = Membership.objects.filter(
        user=user, role="fundos_admin").exists() or user.is_staff
    if not is_admin:
        raise AuthzError()
    return True


class AdminBaseView(APIView):
    permission_classes = [IsAuthenticated]

    def initial(self, request, *args, **kwargs):
        super().initial(request, *args, **kwargs)
        require_platform_admin(request.user)


CONFIG_FIELDS = (
    "smtp_host", "smtp_port", "smtp_user", "smtp_password_env_var",
    "from_email", "reply_to", "otp_option", "whatsapp_phone_number_id",
    "whatsapp_access_token_env_var", "whatsapp_otp_template_name",
    "whatsapp_alert_template_name", "whatsapp_alert_image_template_name",
    "whatsapp_otp_template_lang", "ai_mocked", "ai_guidance_data",
    "ai_guidance_documents", "ai_synthetic_disclosure_enabled",
    "show_numeric_readiness", "enable_research_extensions",
    "enable_instrument_selection", "enable_dilution_simulator",
    "mandatory_ckb_fields", "features",
)


class AppConfigurationView(AdminBaseView):
    def get(self, request):
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        if not cfg:
            cfg = AppConfiguration.objects.create()
        return Response({f: getattr(cfg, f) for f in CONFIG_FIELDS})

    def patch(self, request):
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo() or AppConfiguration.objects.create()
        for field in CONFIG_FIELDS:
            if field in request.data:
                setattr(cfg, field, request.data[field])
        cfg.save()
        return Response({f: getattr(cfg, f) for f in CONFIG_FIELDS})


def _serialise_alert(a):
    return {
        "id": str(a.id), "name": a.name, "sourceType": a.source_type,
        "isActive": a.is_active, "heading": a.heading,
        "eventKey": a.event_key, "metricColumn": a.metric_column,
        "condition": a.condition,
        "thresholdValue": str(a.threshold_value)
        if a.threshold_value is not None else None,
        "messageTemplate": a.insight_user_query,
        "notifyEmail": a.notify_email, "notifyInApp": a.notify_in_app,
        "notifyWhatsapp": a.notify_whatsapp,
        "recipients": [str(u.id) for u in a.recipients_users.all()],
        "recipientPhoneNumbers": a.recipients_phone_numbers,
        "runInterval": a.run_interval,
        "nextRunAt": a.next_run_at.isoformat() if a.next_run_at else None,
        "lastRunAt": a.last_run_at.isoformat() if a.last_run_at else None,
        "lastRunStatus": a.last_run_status,
    }


_ALERT_FIELD_MAP = {
    "name": "name", "sourceType": "source_type", "isActive": "is_active",
    "heading": "heading", "eventKey": "event_key",
    "metricColumn": "metric_column", "condition": "condition",
    "thresholdValue": "threshold_value",
    "messageTemplate": "insight_user_query",
    "notifyEmail": "notify_email", "notifyInApp": "notify_in_app",
    "notifyWhatsapp": "notify_whatsapp",
    "recipientPhoneNumbers": "recipients_phone_numbers",
    "runInterval": "run_interval",
}


def _apply_alert_payload(alert, data):
    for camel, snake in _ALERT_FIELD_MAP.items():
        if camel in data:
            setattr(alert, snake, data[camel])
        elif snake in data:
            setattr(alert, snake, data[snake])
    alert.save()
    if "recipients" in data:
        from fundos.core.models import User
        alert.recipients_users.set(
            User.objects.filter(id__in=data["recipients"]))
    return alert


class AlertDefinitionsView(AdminBaseView):
    def get(self, request):
        from fundos.core.models import AlertDefinition
        return Response({"items": [
            _serialise_alert(a) for a in
            AlertDefinition.objects.prefetch_related("recipients_users")]})

    def post(self, request):
        from fundos.core.models import AlertDefinition
        if not request.data.get("name"):
            raise DomainValidationError("name is required.",
                                        fields={"name": "required"})
        alert = AlertDefinition(created_by=request.user)
        alert = _apply_alert_payload(alert, request.data)
        return Response(_serialise_alert(alert),
                        status=status.HTTP_201_CREATED)


class AlertDefinitionDetailView(AdminBaseView):
    def _get(self, alert_id):
        from fundos.core.models import AlertDefinition
        alert = AlertDefinition.objects.filter(id=alert_id).first()
        if not alert:
            from fundos.core.exceptions import NotFoundInDeal
            raise NotFoundInDeal("Alert not found.")
        return alert

    def get(self, request, alert_id):
        return Response(_serialise_alert(self._get(alert_id)))

    def patch(self, request, alert_id):
        alert = _apply_alert_payload(self._get(alert_id), request.data)
        return Response(_serialise_alert(alert))

    def delete(self, request, alert_id):
        self._get(alert_id).delete()
        return Response(status=status.HTTP_204_NO_CONTENT)


class AlertEvaluateNowView(AdminBaseView):
    """POST /admin/alerts/{id}/evaluate-now — Gyain admin surface (Doc 6 §6.6)."""

    def post(self, request, alert_id):
        from fundos.core.alerting.tasks import evaluate_single_alert
        result = evaluate_single_alert(str(alert_id))
        return Response({"evaluated": True, "result": result})


class LlmEndpointsView(AdminBaseView):
    def get(self, request):
        from fundos.llm.models import LLMEndpoint
        return Response({"items": [{
            "code": e.code, "providerKind": e.provider_kind,
            "baseUrl": e.base_url, "defaultModel": e.default_model,
            "apiKeyEnvVar": e.api_key_env_var,
            "hasKeyInEnv": e.has_api_key_in_env(),
            "isActive": e.is_active, "priority": e.priority,
        } for e in LLMEndpoint.objects.all()]})


class LlmBindingsView(AdminBaseView):
    def get(self, request):
        from fundos.llm.models import LLMRoleBinding
        return Response({"items": [{
            "role": b.role, "primary": b.primary_endpoint_id,
            "fallback": b.fallback_endpoint_id, "isMocked": b.is_mocked,
            "maxOutputTokens": b.max_output_tokens,
        } for b in LLMRoleBinding.objects.all()]})

    def patch(self, request):
        from fundos.llm.models import LLMEndpoint, LLMRoleBinding
        for item in request.data.get("items", []):
            binding = LLMRoleBinding.objects.filter(
                role=item.get("role")).first()
            if not binding:
                continue
            if "primary" in item:
                binding.primary_endpoint = LLMEndpoint.objects.get(
                    code=item["primary"])
            if "fallback" in item:
                binding.fallback_endpoint = (
                    LLMEndpoint.objects.filter(code=item["fallback"]).first()
                    if item["fallback"] else None)
            if "isMocked" in item:
                binding.is_mocked = bool(item["isMocked"])
            binding.save()
        return self.get(request)


class ScoringConfigView(AdminBaseView):
    def get(self, request):
        from fundos.config.models import ScoringConfig
        return Response({"items": [{
            "versionNo": c.version_no, "isActive": c.is_active,
            "createdAt": c.created_at.isoformat(),
        } for c in ScoringConfig.objects.order_by("-version_no")]})

    def post(self, request):
        """Create a new version (optionally activate)."""
        from fundos.config.models import ScoringConfig
        data = request.data.get("data")
        if not isinstance(data, dict):
            raise DomainValidationError("data (object) is required.",
                                        fields={"data": "required"})
        last = ScoringConfig.objects.order_by("-version_no").first()
        version = ScoringConfig.objects.create(
            version_no=(last.version_no + 1) if last else 1,
            data=data, created_by=request.user,
            is_active=bool(request.data.get("activate")))
        if version.is_active:
            ScoringConfig.objects.exclude(id=version.id).update(is_active=False)
        return Response({"versionNo": version.version_no,
                         "isActive": version.is_active},
                        status=status.HTTP_201_CREATED)


class MasterValuesView(AdminBaseView):
    def get(self, request, table):
        from fundos.config.models import MasterValue
        return Response({"items": [{
            "code": v.code, "label": v.label, "meta": v.meta,
            "sortOrder": v.sort_order, "isActive": v.is_active,
        } for v in MasterValue.values(table)]})
