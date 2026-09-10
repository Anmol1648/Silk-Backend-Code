"""
Company logo handling (LLM_ISSUE3, item 3).

The onboarding payload may carry a logo in one of two forms:
  * logoUrl    — a direct URL (e.g. an auto-fetched logo.dev link). Stored
                 as-is on the profile.
  * logoBase64 — a base64-encoded image the user uploaded from their machine.
                 Saved to object storage; the resulting signed URL is stored.

resolve_logo() returns the URL string to persist (or "" if neither is usable),
and never raises — a logo problem must not fail onboarding.
"""
import base64
import binascii
import logging
import uuid

logger = logging.getLogger(__name__)

# Accept the common raster/vector web image types.
_EXT_BY_PREFIX = {
    b"\x89PNG": "png",
    b"\xff\xd8\xff": "jpg",
    b"GIF8": "gif",
    b"RIFF": "webp",       # RIFF....WEBP
    b"<svg": "svg",
    b"<?xm": "svg",         # <?xml ... <svg
}
_MAX_LOGO_BYTES = 5 * 1024 * 1024   # 5 MB cap


def resolve_logo(profile, *, logo_url: str = "", logo_base64: str = "") -> str:
    """Return the logo URL to store on the profile.

    Priority: an explicit logoUrl is validated and stored; otherwise a
    logoBase64 is decoded and saved to storage and its signed URL returned. Any
    failure degrades gracefully to "" so onboarding still succeeds.

    The URL is validated, not trusted
    --------------------------------
    This used to store whatever arrived, ``return url[:1024]``, straight into a
    column the frontend renders as an ``<img src>`` and the exporters embed in
    a PDF. A caller-supplied string reaching an attribute the browser resolves
    is the whole shape of stored XSS: ``javascript:`` and ``data:text/html``
    execute in the viewer's origin, and every teammate who opens the company
    afterwards is a target, not just the submitter. A private-network URL is
    the same problem pointed inward — the exporters fetch images server-side,
    which turns the field into an SSRF trigger.

    So it goes through the same validator the model's URLs do: http(s) only, a
    resolvable public host, length-bounded. A rejected URL is logged with the
    reason and dropped, because a company without a logo renders fine and a
    company with a hostile one does not.
    """
    from fundos.profile.sanitize import clean_url

    raw = (logo_url or "").strip()
    if raw:
        url, problem = clean_url(raw, allow_redirect=True)
        if url:
            return url[:1024]
        logger.warning("LOGO: rejected the supplied logo URL for company %s "
                       "— %s (%.120s)",
                       getattr(profile, "company_id", "?"), problem, raw)
        # Fall through: a bad URL must not also discard a usable upload sent
        # alongside it.

    b64 = (logo_base64 or "").strip()
    if not b64:
        return ""

    try:
        # Tolerate a data URI prefix ("data:image/png;base64,....").
        if b64.startswith("data:"):
            b64 = b64.split(",", 1)[1] if "," in b64 else ""
        raw = base64.b64decode(b64, validate=False)
    except (binascii.Error, ValueError) as e:
        logger.warning("LOGO: base64 decode failed for company %s: %s",
                       getattr(profile, "company_id", "?"), e)
        return ""

    if not raw or len(raw) > _MAX_LOGO_BYTES:
        logger.warning("LOGO: empty or oversized image for company %s",
                       getattr(profile, "company_id", "?"))
        return ""

    ext = _guess_ext(raw)
    remote = f"logos/{profile.company_id}/{uuid.uuid4().hex}.{ext}"
    try:
        from fundos.docs.storage import get_storage
        storage = get_storage()
        if not storage.upload_bytes(raw, remote):
            logger.warning("LOGO: storage upload failed for company %s",
                           getattr(profile, "company_id", "?"))
            return ""
        # Long-lived signed URL so the dashboard/profile can render it.
        return storage.signed_url(remote, expires_sec=315360000)[:1024]  # ~10y
    except Exception as e:
        logger.warning("LOGO: storage error for company %s: %s",
                       getattr(profile, "company_id", "?"), e)
        return ""


def _guess_ext(raw: bytes) -> str:
    head = raw[:4]
    for prefix, ext in _EXT_BY_PREFIX.items():
        if raw[:len(prefix)] == prefix:
            return ext
    # Default to png; storage doesn't care about the extension for signing.
    return "png"
