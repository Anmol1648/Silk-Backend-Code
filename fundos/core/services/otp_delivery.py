"""
Login-OTP delivery (Doc 6 §10 / Appendix E) — channel per
AppConfiguration.otp_option, ported from Gyain `documents/whatsapp_otp.py`.

* email (default): plain notification email via the async email service.
* whatsapp: uses the Meta AUTHENTICATION-category template
  (`whatsapp_otp_template_name`, single {{1}} body parameter + copy-code
  button) — NEVER the alert utility templates. Falls back to email when the
  user has no verified phone or WhatsApp is unconfigured, so login is never
  blocked by a channel problem.

When FUNDOS_OTP_ECHO_CONSOLE is on (dev only), the code is also written to
the server console so a local sign-in does not depend on a mailbox.
"""
import logging

import requests

logger = logging.getLogger(__name__)

GRAPH_API_VERSION = "v21.0"
GRAPH_API_BASE = f"https://graph.facebook.com/{GRAPH_API_VERSION}"


def _otp_whatsapp_config():
    try:
        from fundos.config.models import AppConfiguration
        config = AppConfiguration.get_solo()
        if not config:
            return None
        phone_id = getattr(config, "whatsapp_phone_number_id", None)
        token = config.whatsapp_access_token_value
        template = (config.whatsapp_otp_template_name or "").strip()
        if not phone_id or not token or not template:
            return None
        return {"phone_number_id": phone_id.strip(),
                "access_token": token.strip(),
                "template_name": template,
                "template_lang": (config.whatsapp_otp_template_lang
                                  or "en").strip()}
    except Exception as e:
        logger.error("OTP_DELIVERY: failed to load WhatsApp config: %s", e)
        return None


def send_whatsapp_otp(mobile_number: str, otp_code: str) -> bool:
    """Send an OTP via the approved AUTHENTICATION template. Never raises."""
    cfg = _otp_whatsapp_config()
    if not cfg:
        logger.warning("OTP_DELIVERY: WhatsApp OTP not configured.")
        return False
    from fundos.core.services.whatsapp_service import normalize_msisdn
    to = normalize_msisdn(mobile_number)
    if not to:
        logger.error("OTP_DELIVERY: invalid phone %r", mobile_number)
        return False
    payload = {
        "messaging_product": "whatsapp", "to": to, "type": "template",
        "template": {
            "name": cfg["template_name"],
            "language": {"code": cfg["template_lang"]},
            "components": [
                {"type": "body",
                 "parameters": [{"type": "text", "text": str(otp_code)}]},
                # Copy-code button — required by Meta auth templates.
                {"type": "button", "sub_type": "url", "index": "0",
                 "parameters": [{"type": "text", "text": str(otp_code)}]},
            ],
        },
    }
    url = f"{GRAPH_API_BASE}/{cfg['phone_number_id']}/messages"
    headers = {"Authorization": f"Bearer {cfg['access_token']}",
               "Content-Type": "application/json"}
    try:
        resp = requests.post(url, json=payload, headers=headers, timeout=15)
        if resp.status_code in (200, 201):
            logger.info("OTP_DELIVERY: WhatsApp OTP sent to %s", to)
            return True
        logger.error("OTP_DELIVERY: WhatsApp OTP %s for %s: %s",
                     resp.status_code, to, resp.text[:200])
        return False
    except Exception as e:
        logger.error("OTP_DELIVERY: WhatsApp OTP error for %s: %s", to, e)
        return False


def echo_otp_to_console(identity: str, code: str, ttl_minutes: int) -> None:
    """Print the code to the server console when FUNDOS_OTP_ECHO_CONSOLE is
    set. Off by default and never enabled outside dev — the email/WhatsApp
    channel is the only delivery path everywhere else."""
    from django.conf import settings as dj_settings

    if not getattr(dj_settings, "FUNDOS_OTP_ECHO_CONSOLE", False):
        return
    # Written straight to the console rather than through the JSON logger:
    # this exists to be read by a human watching runserver.
    import sys
    rows = ["LOGIN OTP (dev console delivery)",
            identity,
            f"code: {code}   expires in {ttl_minutes} min"]
    width = max(len(r) for r in rows) + 2
    lines = ["", "  +" + "-" * width + "+",
             f"  | {rows[0]:<{width - 2}} |",
             "  +" + "-" * width + "+"]
    lines += [f"  | {r:<{width - 2}} |" for r in rows[1:]]
    lines += ["  +" + "-" * width + "+", ""]
    print("\n".join(lines), file=sys.stderr, flush=True)


def send_login_otp(user, code: str, ttl_minutes: int) -> str:
    """Deliver a login OTP on the configured channel. Returns the channel
    actually used ('whatsapp' | 'email')."""
    echo_otp_to_console(user.email, code, ttl_minutes)

    channel = "email"
    try:
        from fundos.config.models import AppConfiguration
        config = AppConfiguration.get_solo()
        if config and getattr(config, "otp_option", "email") == "whatsapp":
            channel = "whatsapp"
    except Exception:
        pass

    if channel == "whatsapp" and user.phone_msisdn:
        if send_whatsapp_otp(user.phone_msisdn, code):
            return "whatsapp"
        logger.warning("OTP_DELIVERY: WhatsApp failed for %s — "
                       "falling back to email.", user.email)

    from fundos.core.services.email_service import send_email_async

    # Tester Issue 1: the sign-in email was a bare one-line plain-text
    # message. It now ships a branded HTML template (prominent code, expiry,
    # safety note) with a plain-text fallback for text-only clients.
    text_body = (
        f"Hi {user.name or 'there'},\n\n"
        f"Your FundOS sign-in code is: {code}\n\n"
        f"This code expires in {ttl_minutes} minutes and can only be used "
        f"once.\n\n"
        f"If you didn't request this code, you can safely ignore this "
        f"email — no one can sign in without it.\n\n"
        f"— The FundOS team"
    )
    html_body = f"""\
<!DOCTYPE html>
<html>
<body style="margin:0;padding:0;background:#f4f6f4;font-family:Arial,Helvetica,sans-serif;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f6f4;padding:32px 0;">
    <tr><td align="center">
      <table role="presentation" width="480" cellpadding="0" cellspacing="0"
             style="background:#ffffff;border-radius:10px;border:1px solid #e3e8e3;overflow:hidden;">
        <tr>
          <td style="background:#0d3b2e;padding:20px 32px;">
            <span style="font-size:20px;font-weight:700;color:#ffffff;">Fund<span style="color:#7fd1a7;">OS</span></span>
          </td>
        </tr>
        <tr>
          <td style="padding:32px;">
            <p style="margin:0 0 8px;font-size:16px;color:#1c2b24;">Hi {user.name or 'there'},</p>
            <p style="margin:0 0 20px;font-size:14px;color:#4b5a52;">
              Use this one-time code to sign in to FundOS:
            </p>
            <div style="background:#f0f7f2;border:1px solid #cfe5d6;border-radius:8px;
                        padding:18px;text-align:center;margin:0 0 20px;">
              <span style="font-size:32px;font-weight:700;letter-spacing:8px;color:#0d3b2e;">{code}</span>
            </div>
            <p style="margin:0 0 6px;font-size:13px;color:#4b5a52;">
              This code expires in <strong>{ttl_minutes} minutes</strong> and can only be used once.
            </p>
            <p style="margin:0;font-size:13px;color:#8a968f;">
              Didn't request this? You can safely ignore this email — no one
              can sign in to your account without the code.
            </p>
          </td>
        </tr>
        <tr>
          <td style="padding:16px 32px;border-top:1px solid #eef1ee;">
            <p style="margin:0;font-size:12px;color:#8a968f;">
              FundOS — your fundraising operating system. This is an automated
              message; replies are not monitored.
            </p>
          </td>
        </tr>
      </table>
    </td></tr>
  </table>
</body>
</html>"""
    send_email_async(
        subject="Your FundOS sign-in code",
        body=text_body,
        html_body=html_body,
        to=[user.email])
    return "email"
