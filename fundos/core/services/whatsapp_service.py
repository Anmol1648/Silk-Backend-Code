"""
Generic WhatsApp Business Cloud API sender — ported from Gyain
`gyainapp/whatsapp_service.py` (tried & tested), adapted for FundOS:
single-DB (no tenant alias plumbing), config from fundos.config.

META / WHATSAPP CONSTRAINTS (Doc 6 §5):
  * Business-initiated messages MUST use a pre-approved *Utility* template
    (free-form text only exists inside a 24-hour customer-service window,
    which alerts don't have). Approved templates:
        alert_generic        — body: *{{1}}*\n{{2}}
        alert_generic_image  — same body + IMAGE header component
  * Images are uploaded to the Cloud API /media endpoint first for a media_id.
  * Body param capped at 900 chars — email carries the full text, WhatsApp
    carries summary + link.
"""
import logging
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"
MAX_BODY_PARAM_CHARS = 900


def _get_whatsapp_config():
    """Resolve WhatsApp credentials + alert template names from
    AppConfiguration. Returns None if core credentials are missing
    (caller no-ops with a logged warning — Doc 6 §1)."""
    try:
        from fundos.config.models import AppConfiguration
        config = AppConfiguration.get_solo()
        if not config:
            return None
        phone_id = getattr(config, "whatsapp_phone_number_id", None)
        token = config.whatsapp_access_token_value  # env-var preferred (Doc 6 §1)
        if not phone_id or not token:
            return None
        return {
            "phone_number_id": phone_id.strip(),
            "access_token": token.strip(),
            "alert_template": (config.whatsapp_alert_template_name or "alert_generic").strip(),
            "alert_image_template": (config.whatsapp_alert_image_template_name
                                     or "alert_generic_image").strip(),
            "template_lang": (config.whatsapp_otp_template_lang or "en").strip(),
        }
    except Exception as e:
        logger.error("WHATSAPP_SVC: Failed to load config: %s", e)
        return None


def normalize_msisdn(mobile_number: str) -> str:
    """Digits only; prepend country code 91 for bare 10-digit Indian numbers."""
    cleaned = "".join(filter(str.isdigit, str(mobile_number or "")))
    if not cleaned:
        return ""
    if len(cleaned) == 10:
        cleaned = f"91{cleaned}"
    return cleaned


def _upload_media(cfg, image_bytes: bytes, mime_type: str = "image/png") -> str:
    """Upload an image to /media and return its media id."""
    url = f"{GRAPH_API_BASE}/{cfg['phone_number_id']}/media"
    headers = {"Authorization": f"Bearer {cfg['access_token']}"}
    files = {"file": ("alert_image", image_bytes, mime_type)}
    data = {"messaging_product": "whatsapp", "type": mime_type}
    resp = requests.post(url, headers=headers, files=files, data=data, timeout=30)
    if resp.status_code not in (200, 201):
        raise RuntimeError(f"media upload failed {resp.status_code}: {resp.text[:200]}")
    media_id = resp.json().get("id")
    if not media_id:
        raise RuntimeError(f"media upload returned no id: {resp.text[:200]}")
    return media_id


def send_whatsapp_template(mobile_number: str, heading: str, body: str,
                           image_bytes: bytes = None,
                           image_mime: str = "image/png") -> bool:
    """Send a business-initiated alert to one WhatsApp number using an
    approved utility template. Returns True if accepted. Never raises."""
    cfg = _get_whatsapp_config()
    if not cfg:
        logger.warning("WHATSAPP_SVC: Not configured in Application Settings — skipping.")
        return False

    to = normalize_msisdn(mobile_number)
    if not to:
        logger.error("WHATSAPP_SVC: Invalid phone number: %r", mobile_number)
        return False

    heading = (heading or "Alert").strip()
    body = (body or "").strip()[:MAX_BODY_PARAM_CHARS]

    components = []
    template_name = cfg["alert_template"]

    if image_bytes:
        try:
            media_id = _upload_media(cfg, image_bytes, image_mime)
            template_name = cfg["alert_image_template"]
            components.append({
                "type": "header",
                "parameters": [{"type": "image", "image": {"id": media_id}}],
            })
        except Exception as e:
            # Fail soft: fall back to text-only template rather than dropping.
            logger.warning("WHATSAPP_SVC: image header failed (%s); "
                           "falling back to text template.", e)

    components.append({
        "type": "body",
        "parameters": [
            {"type": "text", "text": heading},
            {"type": "text", "text": body or heading},
        ],
    })

    url = f"{GRAPH_API_BASE}/{cfg['phone_number_id']}/messages"
    headers = {"Authorization": f"Bearer {cfg['access_token']}",
               "Content-Type": "application/json"}
    payload = {
        "messaging_product": "whatsapp",
        "to": to,
        "type": "template",
        "template": {
            "name": template_name,
            "language": {"code": cfg["template_lang"]},
            "components": components,
        },
    }

    logger.info("WHATSAPP_SVC: Sending alert to %s via template '%s'", to, template_name)
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=20)
        if resp.status_code in (200, 201):
            msg_id = resp.json().get("messages", [{}])[0].get("id", "unknown")
            logger.info("WHATSAPP_SVC: Sent to %s. Message ID: %s", to, msg_id)
            return True
        try:
            err = resp.json().get("error", {})
        except Exception:
            err = {}
        logger.error("WHATSAPP_SVC: %s for %s. Error: %s",
                     resp.status_code, to, err.get("message", resp.text[:200]))
        return False
    except requests.exceptions.Timeout:
        logger.error("WHATSAPP_SVC: Timeout for %s", to)
        return False
    except Exception as e:
        logger.error("WHATSAPP_SVC: Error for %s: %s", to, e, exc_info=True)
        return False


# ---------------------------------------------------------------------------
# Async wrapper (Doc 6 §5): Celery task with retry; inline fallback if Celery
# is down; per-number isolation (one bad number never blocks the rest).
# ---------------------------------------------------------------------------

def send_whatsapp_async(to_numbers: List[str], heading: str, body: str,
                        image_bytes: Optional[bytes] = None,
                        image_mime: str = "image/png") -> None:
    try:
        _send_whatsapp_task.delay(list(to_numbers or []), heading, body,
                                  image_bytes, image_mime)
        logger.debug("NOTIFY: queued async whatsapp to %s", to_numbers)
    except Exception as celery_err:
        logger.warning("NOTIFY: Celery unavailable (%s) — sending whatsapp inline",
                       celery_err)
        _send_whatsapp_inline(to_numbers, heading, body, image_bytes, image_mime)


def _send_whatsapp_inline(to_numbers, heading, body, image_bytes, image_mime):
    sent = 0
    for number in (to_numbers or []):
        try:
            if send_whatsapp_template(number, heading, body,
                                      image_bytes=image_bytes,
                                      image_mime=image_mime):
                sent += 1
        except Exception as e:
            logger.error("NOTIFY: whatsapp send failed for %s: %s", number, e)
    logger.info("NOTIFY: whatsapp delivered to %d/%d numbers",
                sent, len(to_numbers or []))
    return sent


try:
    from celery import shared_task

    @shared_task(
        name="fundos.core.services.whatsapp_service.send_whatsapp_task",
        bind=False, max_retries=2, default_retry_delay=60,
        acks_late=True, ignore_result=True,
    )
    def _send_whatsapp_task(to_numbers, heading, body, image_bytes=None,
                            image_mime="image/png"):
        try:
            _send_whatsapp_inline(to_numbers, heading, body, image_bytes, image_mime)
        except Exception as exc:
            logger.warning("NOTIFY: whatsapp attempt failed, will retry: %s", exc)
            raise _send_whatsapp_task.retry(exc=exc)

except ImportError:  # Celery not installed — no-op placeholder
    def _send_whatsapp_task(*args, **kwargs):  # type: ignore[misc]
        pass
