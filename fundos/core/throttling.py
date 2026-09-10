"""
Rate limiting (Gap G15) on sensitive endpoints.

* OTP request/verify — throttled per identity (email) AND per client IP so
  neither an email flood nor a single-IP spray gets through. OTP attempt
  limits per code already exist; this adds request-level protection.
* generate endpoints — a per-user budget that caps LLM spend; couples with
  the existing LLM cost logging and slow/failed-task alerts.

Rates come from settings so each environment can tune them:
  FUNDOS_THROTTLE_OTP_REQUEST   default "6/hour"    (Doc: 6 requests/hour)
  FUNDOS_THROTTLE_OTP_VERIFY    default "30/hour"
  FUNDOS_THROTTLE_GENERATE      default "60/hour"
"""
from django.conf import settings
from rest_framework.throttling import SimpleRateThrottle


class _IdentityIpThrottle(SimpleRateThrottle):
    """Key = lowercase email from the body + client IP."""

    setting_name = ""
    default_rate = "6/hour"

    def get_rate(self):
        # NOTE: never set a `rate` class attribute — SimpleRateThrottle
        # skips get_rate() when one exists, freezing the settings value.
        return getattr(settings, self.setting_name, None) or self.default_rate

    def get_cache_key(self, request, view):
        email = ""
        try:
            email = (request.data.get("email") or "").strip().lower()
        except Exception:
            pass
        ident = f"{email}:{self.get_ident(request)}"
        return self.cache_format % {"scope": self.scope, "ident": ident}


class OtpRequestThrottle(_IdentityIpThrottle):
    scope = "otp_request"
    default_rate = "6/hour"
    setting_name = "FUNDOS_THROTTLE_OTP_REQUEST"


class OtpVerifyThrottle(_IdentityIpThrottle):
    scope = "otp_verify"
    default_rate = "30/hour"
    setting_name = "FUNDOS_THROTTLE_OTP_VERIFY"


class GenerateThrottle(SimpleRateThrottle):
    """Per-user budget on all /…/generate endpoints (LLM cost control)."""

    scope = "generate"

    def get_rate(self):
        return getattr(settings, "FUNDOS_THROTTLE_GENERATE", None) \
            or "60/hour"

    def get_cache_key(self, request, view):
        ident = (str(request.user.id)
                 if request.user and request.user.is_authenticated
                 else self.get_ident(request))
        return self.cache_format % {"scope": self.scope, "ident": ident}
