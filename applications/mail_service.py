"""Sending e-mail, over HTTPS only in production.

Backends (chosen by configuration, see ``backend_name``):
  * ``resend``  - Resend's HTTPS API, when ``RESEND_API_KEY`` is set. (Render's free
                  tier blocks outbound SMTP, so SMTP cannot be the production path.)
  * ``smtp``    - only with an explicit ``EMAIL_BACKEND=smtp`` (local MailHog).
  * ``preview`` - nothing configured: nothing is delivered, the caller stores the
                  rendered message in the sandbox outbox.

``EMAIL_ENABLED=false`` is a kill switch: ``send_email`` raises ``EmailDisabled``.
The standard library only; no extra dependencies.
"""
import json
import logging
import os
import re
import urllib.error
import urllib.request
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from smtplib import SMTP, SMTPException

from flask import current_app

log = logging.getLogger(__name__)

RESEND_URL = "https://api.resend.com/emails"
HTTP_TIMEOUT = 10  # seconds
SENT = "sent"
PREVIEW = "preview"


class MailError(Exception):
    """Delivery failed; the message is safe to show in the job's ``detail``."""


class EmailDisabled(MailError):
    pass


def backend_name(config=None):
    config = config or current_app.config
    if config.get("EMAIL_BACKEND") == "smtp":
        return "smtp"
    if config.get("RESEND_API_KEY") and real_sending_allowed(config):
        return "resend"
    return "preview"


def real_sending_allowed(config):
    """In production, e-mailing real inboxes needs the Turnstile human check (or an explicit
    ALLOW_EMAIL_WITHOUT_TURNSTILE=true). Otherwise the jobs API stays in preview mode."""
    return (not config.get("IS_PRODUCTION") or bool(config.get("TURNSTILE_SECRET_KEY"))
            or bool(config.get("ALLOW_EMAIL_WITHOUT_TURNSTILE")))


def delivery_configured(config=None):
    return backend_name(config) != "preview"


def email_enabled_for_ui(config=None):
    """What ``GET /api/demo/jobs`` reports as ``email_enabled``: real delivery is possible."""
    config = config or current_app.config
    return bool(config.get("EMAIL_ENABLED")) and delivery_configured(config)


def http_post_json(url, payload, headers=None, timeout=HTTP_TIMEOUT):
    """POST JSON and return ``(status, parsed body or {})``. Raises OSError on network failure."""
    request = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "grocery-store-demo/1.0", **(headers or {})},
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            status, raw = response.status, response.read(1_000_000)
    except urllib.error.HTTPError as exc:
        status, raw = exc.code, exc.read(1_000_000)
    try:
        body = json.loads(raw or b"{}")
    except ValueError:
        body = {}
    return status, body if isinstance(body, dict) else {}


def _send_resend(to, subject, html, config):
    payload = {"from": config["EMAIL_FROM"], "to": [to], "subject": subject, "html": html}
    try:
        status, body = http_post_json(RESEND_URL, payload, {"Authorization": f"Bearer {config['RESEND_API_KEY']}"})
    except (OSError, ValueError) as exc:  # URLError, timeouts, connection resets
        raise MailError(f"Could not reach the e-mail provider ({type(exc).__name__})") from None
    if status >= 400:
        reason = str(body.get("message") or body.get("name") or "")[:120]
        raise MailError(f"E-mail provider rejected the message (HTTP {status}{': ' + reason if reason else ''})")


def _send_smtp(to, subject, html, config):
    message = MIMEMultipart()
    message["From"] = config["EMAIL_FROM"]
    message["To"] = to
    message["Subject"] = subject
    message.attach(MIMEText(html, "html"))
    host = os.environ.get("SMTP_HOST") or "localhost"
    port = int(os.environ.get("SMTP_PORT") or 1025)
    try:
        with SMTP(host=host, port=port, timeout=HTTP_TIMEOUT) as client:
            if (os.environ.get("SMTP_USE_TLS") or "").lower() == "true":
                client.starttls()
            if os.environ.get("SMTP_USERNAME") and os.environ.get("SMTP_PASSWORD"):
                client.login(os.environ["SMTP_USERNAME"], os.environ["SMTP_PASSWORD"])
            client.send_message(message)
    except (OSError, SMTPException) as exc:
        raise MailError(f"SMTP delivery failed ({type(exc).__name__})") from None


_HEADER_INJECTION = re.compile(r"[\r\n]")


def send_email(to, subject, html):
    """Deliver one message. Returns ``"sent"`` or ``"preview"`` (nothing configured).
    Raises ``EmailDisabled`` (kill switch) or ``MailError``. Needs an app context."""
    config = current_app.config
    if not config.get("EMAIL_ENABLED"):
        raise EmailDisabled("e-mail disabled")
    if _HEADER_INJECTION.search(to) or _HEADER_INJECTION.search(subject):
        raise MailError("Invalid recipient or subject")
    backend = backend_name(config)
    if backend == "resend":
        _send_resend(to, subject, html, config)
    elif backend == "smtp":
        _send_smtp(to, subject, html, config)
    else:
        return PREVIEW
    return SENT
