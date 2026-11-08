"""Demo e-mail endpoints: the visitor address, jobs and the outbox. Registered on
`current_app` at import time (rate limits are applied in app.py)."""
import logging
import re

from flask import abort, current_app as app, g, jsonify, request
from flask_jwt_extended import get_jwt_identity, verify_jwt_in_request
from flask_jwt_extended.exceptions import JWTExtendedException
from jwt.exceptions import PyJWTError

from applications import backend_tasks
from applications.client_key import client_key
from applications.database import get_db
from applications.demo_routes import OUTBOX_HTML_MAX, OUTBOX_RESPONSE_BUDGET, TRUNCATED_MARK, _display_cell
from applications.email_domain import domain_receives_mail
from applications.email_limits import EmailLimitReached, canonical_recipient, release, reserve
from applications.http_errors import ApiError, BadRequest, Unauthorized, api_errors, json_body
from applications.mail_service import delivery_configured, email_enabled_for_ui, http_post_json

log = logging.getLogger(__name__)

TURNSTILE_URL = "https://challenges.cloudflare.com/turnstile/v0/siteverify"
MAX_EMAIL_LENGTH = 254
# The local part is deliberately tighter than RFC 5322: only [a-z0-9._+-], no leading/trailing
# dot, no "..". Exotic quoted/symbol addresses are not worth the bounce and canonicalisation risk.
_EMAIL_RE = re.compile(
    r"(?![.])(?!.*\.\.)(?!.*\.@)[a-z0-9._+-]{1,64}@"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,24}"
)


@app.before_request
def _email_endpoints_need_demo_mode():
    if not app.config["DEMO_MODE"] and (request.endpoint or "") in ("demo_email", "demo_jobs_create", "demo_jobs_list",
                                                                    "demo_outbox"):
        abort(404)


class QueueUnavailable(ApiError):
    status = 503


def clean_email(value):
    if not isinstance(value, str):
        raise BadRequest("Enter a valid e-mail address")
    email = value.strip().lower()
    if (len(email) > MAX_EMAIL_LENGTH or ".." in email or not _EMAIL_RE.fullmatch(email)
            or email.startswith(".") or "@." in email or email.split("@")[0].endswith(".")):
        raise BadRequest("Enter a valid e-mail address")
    if not canonical_recipient(email).split("@")[0]:  # e.g. "+tag@example.com"
        raise BadRequest("Enter a valid e-mail address")
    return email


def check_domain(email):
    """Refuse addresses whose domain cannot receive mail (bounces hurt our sender reputation).
    Unknown (DNS timeout) is accepted."""
    if app.config["EMAIL_DOMAIN_CHECK"] and not domain_receives_mail(email.rpartition("@")[2]):
        raise BadRequest("That e-mail domain doesn't seem to receive mail.")


def check_turnstile(token):
    secret = app.config["TURNSTILE_SECRET_KEY"]
    if not secret:
        return
    if not isinstance(token, str) or not token.strip() or len(token) > 4096:
        raise BadRequest("Please complete the human check")
    payload = {"secret": secret, "response": token}
    if request.remote_addr:
        payload["remoteip"] = request.remote_addr
    try:
        status, body = http_post_json(TURNSTILE_URL, payload, timeout=8)
    except (OSError, ValueError):
        raise QueueUnavailable("Could not verify the human check. Please try again.") from None
    if status != 200 or body.get("success") is not True:
        raise BadRequest("The human check failed. Please try again.")
    expected = app.config["TURNSTILE_EXPECTED_HOSTNAME"]
    if expected and str(body.get("hostname", "")).lower() != expected.lower():
        # A token solved on another site's widget (or replayed from elsewhere).
        log.warning("Turnstile hostname mismatch: got %r, expected %r", body.get("hostname"), expected)
        raise BadRequest("The human check failed. Please try again.")


def visitor_email(db=None):
    row = (db or get_db()).execute("SELECT value FROM meta WHERE key = 'visitor_email'").fetchone()
    return row[0] if row else None


def _job_dict(row):
    keys = ("id", "kind", "status", "to_email", "created_at", "updated_at", "detail")
    return dict(zip(keys, row))


_JOB_COLUMNS = "id, kind, status, to_email, created_at, updated_at, detail"


def enqueue_job(kind, account=None, order_id=None):
    """Count the limits, create the ``queued`` job row and dispatch it. Returns the job dict.
    Raises EmailLimitReached, BadRequest (no visitor address) or QueueUnavailable."""
    db = get_db()
    to_email = visitor_email(db)
    if not to_email:
        raise BadRequest("Enter your e-mail address first")
    try:
        token = reserve(app, to_email, client_key(), count_global=delivery_configured())
    except EmailLimitReached:
        raise
    except Exception:  # the counter store (Redis) is down
        log.exception("Could not count the e-mail limits")
        raise QueueUnavailable("The job queue is unavailable right now. Please try again shortly.") from None
    try:
        now = backend_tasks.now_iso()
        cursor = db.execute("INSERT INTO jobs (kind, status, to_email, created_at, updated_at) "
                            "VALUES (?, 'queued', ?, ?, ?)", (kind, to_email, now, now))
        job_id = cursor.lastrowid
        db.commit()
    except Exception:
        release(app, token)
        raise
    job = _job_dict(db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs WHERE id = ?", (job_id,)).fetchone())
    try:
        backend_tasks.dispatch(app, g.sandbox_id, job_id, kind, account, order_id)
    except Exception:
        log.exception("Could not enqueue job %s", job_id)
        release(app, token)
        db.execute("UPDATE jobs SET status = 'failed', detail = ?, updated_at = ? WHERE id = ?",
                   ("The job queue is unavailable", backend_tasks.now_iso(), job_id))
        db.commit()
        raise QueueUnavailable("The job queue is unavailable right now. Please try again shortly.") from None
    return job


def enqueue_order_confirmation(order_id):
    """Checkout hook: never raises, never delays or fails the checkout."""
    try:
        if not app.config["DEMO_MODE"] or not visitor_email():
            return
        enqueue_job("order_confirmation", order_id=order_id)
    except EmailLimitReached as exc:
        log.info("Order confirmation not queued: %s", exc.message)
    except Exception:
        log.exception("Could not queue the order confirmation")


@app.post("/api/demo/email")
@api_errors
def demo_email():
    data = json_body()
    email = clean_email(data.get("email"))
    check_turnstile(data.get("turnstile_token"))
    check_domain(email)
    db = get_db()
    db.execute("INSERT INTO meta (key, value) VALUES ('visitor_email', ?) "
               "ON CONFLICT(key) DO UPDATE SET value = excluded.value", (email,))
    db.commit()
    return jsonify({"email": email})


@app.post("/api/demo/jobs")
@api_errors
def demo_jobs_create():
    kind = json_body().get("kind")
    if kind not in backend_tasks.USER_KINDS:
        raise BadRequest('kind must be "monthly_report" or "daily_reminder"')
    account = None
    if request.headers.get("Authorization"):
        try:
            verify_jwt_in_request(optional=True)
        except (PyJWTError, JWTExtendedException):
            raise Unauthorized("Invalid or expired authorization token") from None  # not a 500
        account = get_jwt_identity()
    return jsonify({"job": enqueue_job(kind, account=account)}), 202


@app.get("/api/demo/jobs")
@api_errors
def demo_jobs_list():
    db = get_db()
    backend_tasks.fail_stale_jobs(db, app.config["JOB_STALE_SECONDS"])
    rows = db.execute(f"SELECT {_JOB_COLUMNS} FROM jobs ORDER BY id DESC LIMIT 50").fetchall()
    return jsonify({"jobs": [_job_dict(r) for r in rows], "email": visitor_email(db),
                    "email_enabled": email_enabled_for_ui()})


@app.get("/api/demo/outbox")
@api_errors
def demo_outbox():
    """Stored e-mails, newest first. Bounded: one body is cut to OUTBOX_HTML_MAX and the
    bodies together to OUTBOX_RESPONSE_BUDGET (further messages come back with an empty
    html and ``truncated: true``)."""
    cursor = get_db().execute(
        "SELECT id, job_id, to_email, subject, html, created_at FROM outbox ORDER BY id DESC LIMIT 50")
    messages, used = [], 0
    for id_, job_id, to_email, subject, html, created_at in cursor:
        subject = _display_cell(subject, 300)[0]
        to_email = _display_cell(to_email, 320)[0]
        truncated = False
        html = html if isinstance(html, str) else str(html)
        room = OUTBOX_RESPONSE_BUDGET - used
        limit = min(OUTBOX_HTML_MAX, max(room, 0))
        if len(html) > limit:
            html = html[:limit] + (TRUNCATED_MARK if limit else "")
            truncated = True
        used += len(html)
        messages.append({"id": id_, "job_id": job_id, "to_email": to_email, "subject": subject, "html": html,
                         "created_at": created_at, "truncated": truncated})
    cursor.close()
    return jsonify({"messages": messages})
