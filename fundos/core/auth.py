"""
Auth: IdpJWTAuthentication (tenant fixed AT LOGIN, carried in the JWT —
Doc 4 §0) + token issuing helpers for the email-OTP flow.
"""
import time

import jwt
from django.conf import settings
from rest_framework.authentication import BaseAuthentication
from rest_framework.exceptions import AuthenticationFailed

from fundos.core import scoping


def issue_tokens(user):
    now = int(time.time())
    base = {
        "sub": str(user.id),
        "email": user.email,
        "tenant_id": str(user.tenant_id) if user.tenant_id else None,
        "iat": now,
    }
    access = jwt.encode(
        {**base, "type": "access", "exp": now + settings.FUNDOS_JWT_ACCESS_TTL_SECONDS},
        settings.FUNDOS_JWT_SECRET, algorithm=settings.FUNDOS_JWT_ALGORITHM)
    refresh = jwt.encode(
        {**base, "type": "refresh", "exp": now + settings.FUNDOS_JWT_REFRESH_TTL_SECONDS},
        settings.FUNDOS_JWT_SECRET, algorithm=settings.FUNDOS_JWT_ALGORITHM)
    return access, refresh


def decode_token(token, expected_type="access"):
    payload = jwt.decode(token, settings.FUNDOS_JWT_SECRET,
                         algorithms=[settings.FUNDOS_JWT_ALGORITHM])
    if payload.get("type") != expected_type:
        raise jwt.InvalidTokenError("wrong token type")
    return payload


class IdpJWTAuthentication(BaseAuthentication):
    """Bearer JWT auth. Sets the thread-local tenant from the token claim —
    the tenant is NEVER derived from a URL/subdomain (Doc 2 §0.2)."""

    keyword = "Bearer"

    def authenticate(self, request):
        header = request.META.get("HTTP_AUTHORIZATION", "")
        if not header.startswith(self.keyword + " "):
            return None
        token = header[len(self.keyword) + 1:].strip()
        try:
            payload = decode_token(token, "access")
        except jwt.ExpiredSignatureError:
            raise AuthenticationFailed("Token expired.")
        except jwt.InvalidTokenError:
            raise AuthenticationFailed("Invalid token.")

        from fundos.core.models import User
        try:
            user = User.objects.get(id=payload["sub"], is_deleted=False, is_active=True)
        except User.DoesNotExist:
            raise AuthenticationFailed("Unknown user.")

        scoping.set_current_tenant(payload.get("tenant_id") or user.tenant_id)
        return (user, payload)
