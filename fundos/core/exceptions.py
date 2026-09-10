"""
Error taxonomy & envelope (Doc 4 §0 / Doc 8 §4).

Standard envelope: {"error": CODE, "detail": msg, "fields": {...}}.
Domain exceptions are typed and raised from services — never craft
responses in views.
"""
import logging

from rest_framework import status
from rest_framework.exceptions import (
    APIException, AuthenticationFailed, NotAuthenticated, NotFound,
    PermissionDenied, Throttled, ValidationError,
)
from rest_framework.response import Response
from rest_framework.views import exception_handler as drf_exception_handler

logger = logging.getLogger(__name__)


class FundosError(APIException):
    code = "E-INTERNAL-500"
    status_code = status.HTTP_500_INTERNAL_SERVER_ERROR
    default_detail = "Internal error."

    def __init__(self, detail=None, fields=None):
        # A {field: message} mapping is a natural way to raise a validation
        # error, and several call sites do exactly that. Without this, the
        # dict landed in `detail` and was rendered as a Python/DRF repr
        # ("{'websiteUrl': ErrorDetail(string=...)}") while `fields` stayed
        # empty — unreadable for a user and useless to a client trying to
        # attach errors to inputs. Split it instead.
        if isinstance(detail, dict) and detail:
            merged = {k: str(v) for k, v in detail.items()}
            if fields:
                merged.update({k: str(v) for k, v in fields.items()})
            fields = merged
            detail = " ".join(str(v) for v in merged.values())
        super().__init__(detail or self.default_detail)
        self.fields = fields or {}


class DomainValidationError(FundosError):
    code = "E-VAL-422"
    status_code = 422
    default_detail = "Validation failed."


class AuthzError(FundosError):
    code = "E-AUTHZ-403"
    status_code = 403
    default_detail = "Forbidden."   # generic — never reveal existence (VAL-M0-001)


class NotFoundInDeal(FundosError):
    code = "E-NOTFOUND-404"
    status_code = 404
    default_detail = "Resource not found in the active deal."


class ConflictError(FundosError):
    code = "E-CONFLICT-409"
    status_code = 409
    default_detail = "Precondition or state conflict."


class GateLocked(FundosError):
    code = "E-GATE-423"
    status_code = 423
    default_detail = "Hard gate unmet."


class PendingRecalc(FundosError):
    code = "E-PENDING-409"
    status_code = 409
    default_detail = "Artefact pending recalculation; refresh required."


class LLMUnavailable(FundosError):
    code = "E-LLM-502"
    status_code = 502
    default_detail = "AI provider unavailable — retry or fallback."


class UngroundableCall(LLMUnavailable):
    """A role that REQUIRES retrieval cannot perform it on this call.

    v27.4. Distinct from LLMUnavailable because the correct response is the
    opposite one. `LLMUnavailable` means "the provider is down, try the other
    path"; the profile generator used to have a second path, and falling back
    to it converted a refusal into a quieter version of the same ungrounded
    profile, because that path did not search either.

    Raised only where the failure is structural rather than transient — an
    unseeded advanced-tier profile, or an open search breaker on a
    search-required role. Retrying will not help; an administrator must act.

    The company-profile pipeline now has one path and no fallback, so this
    surfaces as a failed research batch: the batch records the refusal in the
    dossier, and if no batch searched at all the grounding gate abandons the
    run with the reason on screen rather than publishing recollection.
    """
    code = "E-LLM-UNGROUNDABLE-502"
    default_detail = ("This role cannot answer without web search, and search "
                      "is not available on this call.")


class ResearchAdapterError(FundosError):
    code = "E-ENRICH-502"
    status_code = 502
    default_detail = "Research adapter unreachable."


class UploadTypeError(FundosError):
    code = "E-UPLOAD-415"
    status_code = 415
    default_detail = "Unsupported file type."


class UploadSizeError(FundosError):
    code = "E-UPLOAD-413"
    status_code = 413
    default_detail = "File too large."


class RateLimited(FundosError):
    code = "E-RATE-429"
    status_code = 429
    default_detail = "Rate limited — back off and retry."


class IdempotencyConflict(FundosError):
    code = "E-IDEMP-409"
    status_code = 409
    default_detail = "Idempotency key reused with a different body."


def fundos_exception_handler(exc, context):
    """Map DRF/domain exceptions onto the Doc 4 envelope."""
    response = drf_exception_handler(exc, context)

    if isinstance(exc, FundosError):
        return Response(
            {"error": exc.code, "detail": str(exc.detail), "fields": exc.fields},
            status=exc.status_code,
        )

    if response is not None:
        if isinstance(exc, ValidationError):
            return Response(
                {"error": "E-VAL-422", "detail": "Validation failed.",
                 "fields": response.data if isinstance(response.data, dict) else {"non_field": response.data}},
                status=422,
            )
        if isinstance(exc, (NotAuthenticated, AuthenticationFailed)):
            return Response({"error": "E-AUTH-401", "detail": str(exc.detail), "fields": {}},
                            status=401)
        if isinstance(exc, PermissionDenied):
            return Response({"error": "E-AUTHZ-403", "detail": "Forbidden.", "fields": {}},
                            status=403)
        if isinstance(exc, NotFound):
            return Response({"error": "E-NOTFOUND-404", "detail": str(exc.detail), "fields": {}},
                            status=404)
        if isinstance(exc, Throttled):
            return Response({"error": "E-RATE-429", "detail": str(exc.detail), "fields": {}},
                            status=429)
        # generic DRF error
        detail = response.data.get("detail") if isinstance(response.data, dict) else str(response.data)
        return Response({"error": f"E-HTTP-{response.status_code}",
                         "detail": str(detail), "fields": {}},
                        status=response.status_code)

    logger.exception("Unhandled exception in %s", context.get("view"))
    return None  # let Django 500 machinery run for truly unhandled errors


class GenerationAlreadyRunning(Exception):
    """A generation is already in flight for this profile.

    Raised instead of queueing so a second request does not duplicate work
    already in progress, and — more importantly — so two runs never
    interleave their writes against the same sections.
    """
