"""
Middleware (Doc 6 Appendix G FundOS variant).

RequestIDMiddleware — early: assigns a request id and clears thread context.
IdempotencyMiddleware — POST with Idempotency-Key; key = (tenant, user, path,
key); store sha256(body) + serialized response, TTL 24h. Same key+body ⇒
replay stored response; same key+different body ⇒ 409 E-IDEMP-409 (Doc 8 §4).

Tenant/user aren't resolved until DRF authentication runs, so the middleware
stamps the request and performs the replay/store around the whole response
using the resolved user after the view (request.user is populated by then for
session auth, and JWT auth writes the tenant into thread-local context).
"""
import hashlib
import json
import logging
import uuid

from django.conf import settings
from django.core.cache import cache

from fundos.core import scoping

logger = logging.getLogger(__name__)


class RequestIDMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        rid = request.headers.get("X-Request-ID") or uuid.uuid4().hex[:16]
        scoping.set_request_id(rid)
        scoping.set_current_tenant(None)
        scoping.set_current_deal(None)
        request.request_id = rid
        try:
            response = self.get_response(request)
            response["X-Request-ID"] = rid
            return response
        finally:
            scoping.set_request_id(None)
            scoping.set_current_tenant(None)
            scoping.set_current_deal(None)


class IdempotencyMiddleware:
    HEADER = "Idempotency-Key"

    def __init__(self, get_response):
        self.get_response = get_response
        self.ttl = getattr(settings, "FUNDOS_IDEMPOTENCY_TTL_SECONDS", 86400)

    def _cache_key(self, request, key):
        tenant = scoping.get_current_tenant() or "anon"
        user = getattr(getattr(request, "user", None), "id", None) or "anon"
        return f"idem:{tenant}:{user}:{request.path}:{key}"

    def __call__(self, request):
        key = request.headers.get(self.HEADER)
        if not key or request.method not in ("POST", "PUT", "PATCH"):
            return self.get_response(request)

        body = request.body or b""
        body_hash = hashlib.sha256(body).hexdigest()

        # Pre-auth replay check on the anonymous scope is skipped; the JWT
        # auth class re-binds tenant during the view, so we check after
        # calling downstream but before returning — and short-circuit when a
        # stored record exists for the fully-resolved identity.
        pre_key = self._cache_key(request, key)
        stored = cache.get(pre_key)
        if stored:
            return self._resolve(stored, body_hash)

        response = self.get_response(request)

        # After the view, tenant/user are resolved — check again & store.
        post_key = self._cache_key(request, key)
        if post_key != pre_key:
            stored = cache.get(post_key)
            if stored:
                return self._resolve(stored, body_hash)

        if 200 <= response.status_code < 500:
            try:
                cache.set(post_key, {
                    "body_sha256": body_hash,
                    "status": response.status_code,
                    "content": response.content.decode("utf-8", errors="replace"),
                    "content_type": response.headers.get("Content-Type", "application/json"),
                }, timeout=self.ttl)
            except Exception as e:  # never fail the request over the cache
                logger.warning("IDEMP: store failed: %s", e)
        return response

    @staticmethod
    def _resolve(stored, body_hash):
        from django.http import HttpResponse
        if stored.get("body_sha256") != body_hash:
            return HttpResponse(
                json.dumps({"error": "E-IDEMP-409",
                            "detail": "Idempotency key reused with a different body.",
                            "fields": {}}),
                status=409, content_type="application/json")
        resp = HttpResponse(stored["content"], status=stored["status"],
                            content_type=stored.get("content_type", "application/json"))
        resp["X-Idempotent-Replay"] = "true"
        return resp
