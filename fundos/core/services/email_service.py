"""
Email service (async, tenant-safe) — ported from Gyain
`gyainapp/notification_service.py` per Doc 6 §4.

Entry point signature implemented exactly as specified:
    send_email_async(subject, body, to, cc=None, html_body=None,
                     attachments=None, using=None)

Behaviour:
  * queues a dedicated Celery task and returns immediately; if Celery is
    unreachable, falls back to inline send with a warning — notification
    failure must NEVER fail a business flow;
  * SMTP settings from AppConfiguration (Doc 6 §1); password via env-var name;
  * supports inline images via CID (4-tuple attachments) for alert
    beautification;
  * retries with backoff inside the task; terminal failures logged and
    emit `fundos.email.failed`;
  * `using` (Gyain's tenant-DB alias) is accepted-and-ignored for drop-in
    compatibility of ported code (single-DB FundOS).
"""
import logging
import os
from email.mime.image import MIMEImage
from typing import List, Optional

logger = logging.getLogger(__name__)


def send_email_async(subject: str, body: str, to: List[str],
                     cc: Optional[List[str]] = None,
                     html_body: Optional[str] = None,
                     attachments: Optional[list] = None,   # [(filename, bytes, mime[, cid])]
                     using: Optional[str] = None) -> None:
    try:
        _send_email_task.delay(subject, body, list(to or []), cc or [],
                               html_body, attachments or [], using)
        logger.debug("NOTIFY: queued async email '%s' to %s", subject, to)
    except Exception as celery_err:
        logger.warning("NOTIFY: Celery unavailable (%s) — sending email inline",
                       celery_err)
        _send_email(subject, body, to, cc=cc, html_body=html_body,
                    attachments=attachments, using=using)


try:
    from celery import shared_task

    @shared_task(
        name="fundos.core.services.email_service.send_email_task",
        bind=False, max_retries=2, default_retry_delay=60,
        acks_late=True, ignore_result=True,
    )
    def _send_email_task(subject, body, to, cc=None, html_body=None,
                         attachments=None, using=None):
        try:
            ok = _send_email(subject, body, to, cc=cc or [], html_body=html_body,
                             attachments=attachments or [], using=using)
            if not ok:
                raise RuntimeError("email send returned False")
        except Exception as exc:
            logger.warning("NOTIFY: send attempt failed, will retry: %s", exc)
            raise _send_email_task.retry(exc=exc)

except ImportError:
    def _send_email_task(*args, **kwargs):  # type: ignore[misc]
        pass


def _smtp_connection_kwargs():
    """Resolve the SMTP transport from AppConfiguration (Doc 6 §1) with a
    settings fallback. Password resolved via env-var NAME (never DB value)."""
    try:
        from fundos.config.models import AppConfiguration
        cfg = AppConfiguration.get_solo()
        if cfg and cfg.smtp_host:
            password = ""
            if cfg.smtp_password_env_var:
                password = os.environ.get(cfg.smtp_password_env_var, "")
            return {
                "host": cfg.smtp_host,
                "port": int(cfg.smtp_port or 587),
                "username": cfg.smtp_user or "",
                "password": password,
                "use_tls": True,
            }, (cfg.from_email or None), (cfg.reply_to or None)
    except Exception as e:
        logger.debug("NOTIFY: AppConfiguration SMTP unavailable (%s)", e)
    return None, None, None


def _resolve_from_email() -> str:
    _, from_email, _ = _smtp_connection_kwargs()
    if from_email:
        return from_email
    try:
        from django.conf import settings as dj
        return getattr(dj, "DEFAULT_FROM_EMAIL", None) or "noreply@fundos.in"
    except Exception:
        return "noreply@fundos.in"


def _send_email(subject: str, body: str, to: List[str],
                cc: Optional[List[str]] = None,
                html_body: Optional[str] = None,
                attachments: Optional[list] = None,
                using: Optional[str] = None) -> bool:
    """Send synchronously. Returns True on success. Never raises."""
    if not to:
        return False
    try:
        from django.core.mail import EmailMultiAlternatives, get_connection

        conn_kwargs, from_email, reply_to = _smtp_connection_kwargs()
        connection = None
        if conn_kwargs:
            connection = get_connection(
                backend="django.core.mail.backends.smtp.EmailBackend", **conn_kwargs)
        from_email = from_email or _resolve_from_email()

        msg = EmailMultiAlternatives(
            subject=subject, body=body or subject, from_email=from_email,
            to=list(to), cc=list(cc or []),
            reply_to=[reply_to] if reply_to else None,
            connection=connection,
        )
        if html_body:
            msg.attach_alternative(html_body, "text/html")
            msg.mixed_subtype = "related"
        for att in (attachments or []):
            if len(att) == 4:
                # 4-tuple (filename, content, mimetype, cid) — inline MIMEImage
                filename, content, mimetype, cid = att
                image = MIMEImage(content)
                image.add_header("Content-ID", f"<{cid}>")
                image.add_header("Content-Disposition", "inline", filename=filename)
                msg.attach(image)
            else:
                filename, content, mimetype = att
                msg.attach(filename, content, mimetype)
        msg.send(fail_silently=False)
        logger.info("NOTIFY: email '%s' sent to %s", subject, to)
        return True
    except Exception as e:
        logger.error("NOTIFY: email send failed for '%s' to %s: %s",
                     subject, to, e, exc_info=True)
        try:
            from fundos.core.alerting.tasks import emit_alert
            emit_alert("fundos.email.failed",
                       {"subject": subject, "error": str(e)[:200]})
        except Exception:
            pass
        return False
