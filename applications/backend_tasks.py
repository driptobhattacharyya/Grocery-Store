"""Background e-mail jobs for the demo.

A job is a row in the sandbox's ``jobs`` table: queued -> running -> sent | preview | failed.
The rendered message goes into the sandbox's ``outbox`` table whether it was delivered or
only previewed. Tasks receive the sandbox id and bind it explicitly (there is no request),
so a task never mails anybody but the visitor address stored for that sandbox.

How a job runs (``JOBS_EXECUTION``): ``celery`` (a worker on the REDIS_URL broker),
``thread`` (a small in-process pool, the default without Redis) or ``inline`` (tests).

The old tasks that e-mailed every registered user were removed: in a public demo they
would mail whatever addresses visitors typed in at sign-up.
"""
import datetime
import logging
import sys
from concurrent.futures import ThreadPoolExecutor

from celery import shared_task

from applications.database import get_db
from applications.email_templates import render, safe_account, safe_product_name, safe_unit
from applications.http_errors import is_sqlite_full
from applications.mail_service import PREVIEW, EmailDisabled, MailError, send_email
from applications.sandbox import SandboxExpired, bind_sandbox

log = logging.getLogger(__name__)

KINDS = ("monthly_report", "daily_reminder", "order_confirmation")
USER_KINDS = ("monthly_report", "daily_reminder")  # what POST /api/demo/jobs accepts
DEFAULT_REPORT_ACCOUNT = "demo@grocery.app"


def now_iso(offset_seconds=0):
    moment = datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(seconds=offset_seconds)
    return moment.strftime("%Y-%m-%dT%H:%M:%SZ")


STALE_DETAIL = "Timed out; please try again."


def fail_stale_jobs(db, stale_seconds):
    """Mark jobs that sat in ``queued``/``running`` for longer than ``stale_seconds`` as failed
    (a lost Celery message, a crashed worker, a flushed Redis). Returns how many."""
    now = now_iso()
    cursor = db.execute(
        "UPDATE jobs SET status = 'failed', detail = ?, updated_at = ? "
        "WHERE status IN ('queued', 'running') AND updated_at < ?",
        (STALE_DETAIL, now, now_iso(-stale_seconds)))
    db.commit()
    return cursor.rowcount


# --- message content (reads the bound sandbox) ---------------------------------------

def _order_confirmation(order_id):
    db = get_db()
    order = db.execute("SELECT total_price FROM orders WHERE orderID = ?", (order_id,)).fetchone()
    if order is None:
        raise MailError("Order not found")
    rows = db.execute(
        """SELECT oi.product_name, oi.quantity, COALESCE(p.price, 0)
           FROM order_items oi LEFT JOIN products p ON p.name = oi.product_name
           WHERE oi.orderID = ? ORDER BY oi.order_itemID""", (order_id,)).fetchall()
    items = [{"name": safe_product_name(n), "quantity": q, "line_total": (q or 0) * price} for n, q, price in rows]
    return render("order_confirmation", order_id=order_id, items=items, total=float(order[0] or 0))


def _monthly_report(account):
    """Orders of ``account`` (the identity that asked for the report, or the demo shopper)
    over the last 30 days."""
    since = (datetime.date.today() - datetime.timedelta(days=30)).isoformat()
    rows = get_db().execute(
        "SELECT orderID, order_date, total_price FROM orders WHERE email = ? AND order_date >= ? "
        "ORDER BY orderID DESC LIMIT 25", (account, since)).fetchall()
    orders = [{"id": i, "date": d, "total": float(t or 0)} for i, d, t in rows]
    return render("monthly_report", account=safe_account(account), orders=orders, total=sum(o["total"] for o in orders))


def _daily_reminder():
    rows = get_db().execute(
        """SELECT p.productID, p.name, p.price, p.unit FROM products p JOIN inventory i ON i.productID = p.productID
           WHERE i.quantity > 0 ORDER BY p.productID LIMIT 5""").fetchall()
    return render("daily_reminder", products=[
        {"name": safe_product_name(n, pid), "price": float(p), "unit": safe_unit(u, pid)} for pid, n, p, u in rows])


def build_message(kind, account=None, order_id=None):
    if kind == "order_confirmation":
        return _order_confirmation(order_id)
    if kind == "monthly_report":
        return _monthly_report(account or DEFAULT_REPORT_ACCOUNT)
    if kind == "daily_reminder":
        return _daily_reminder()
    raise MailError("Unknown job kind")


# --- running a job ----------------------------------------------------------------------

def _set_status(job_id, status, detail=None):
    db = get_db()
    db.execute("UPDATE jobs SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
               (status, detail, now_iso(), job_id))
    db.commit()


def execute_job(sandbox_id, job_id, kind, account=None, order_id=None):
    """Run one job. Needs an app context (Celery's FlaskTask and the thread path provide it)."""
    try:
        bind_sandbox(sandbox_id)
    except SandboxExpired:
        log.info("Job %s skipped: sandbox %s... expired", job_id, str(sandbox_id)[:4])
        return "expired"
    try:
        db = get_db()
        row = db.execute("SELECT to_email FROM jobs WHERE id = ?", (job_id,)).fetchone()
        if row is None:
            log.info("Job %s not found in sandbox %s...", job_id, str(sandbox_id)[:4])
            return "missing"
        to_email = row[0]
        # Idempotency guard: claim the job atomically. A task that Celery redelivers (lost ack,
        # visibility timeout, restart) finds the job already running/finished/timed-out and must
        # not send a second e-mail.
        claimed = db.execute("UPDATE jobs SET status = 'running', updated_at = ? WHERE id = ? AND status = 'queued'",
                             (now_iso(), job_id))
        db.commit()
        if claimed.rowcount != 1:
            log.info("Job %s skipped: not queued any more (duplicate delivery?)", job_id)
            return "duplicate"
        try:
            subject, html = build_message(kind, account, order_id)
            outcome = send_email(to_email, subject, html)
        except EmailDisabled as exc:
            _set_status(job_id, "failed", str(exc))
            return "failed"
        except MailError as exc:
            _set_status(job_id, "failed", str(exc))
            return "failed"
        status = "preview" if outcome == PREVIEW else "sent"
        detail = ("No e-mail provider configured: the message is only stored in the outbox."
                  if status == "preview" else "Delivered to the e-mail provider.")
        now = now_iso()
        db.execute("INSERT INTO outbox (job_id, to_email, subject, html, created_at) VALUES (?, ?, ?, ?, ?)",
                   (job_id, to_email, subject, html, now))
        db.execute("UPDATE jobs SET status = ?, detail = ?, updated_at = ? WHERE id = ?",
                   (status, detail, now, job_id))
        db.commit()
        return status
    except SandboxExpired:
        log.info("Job %s: sandbox %s... expired while running", job_id, str(sandbox_id)[:4])
        return "expired"
    except Exception:
        log.exception("Job %s crashed", job_id)
        try:
            _set_status(job_id, "failed", "This demo sandbox is full. Reset the demo to start fresh."
                        if is_sqlite_full(sys.exc_info()[1]) else "Unexpected error while sending")
        except Exception:
            log.exception("Could not mark job %s as failed", job_id)
        return "failed"


@shared_task(name="grocery.run_email_job", ignore_result=True)
def run_email_job(sandbox_id, job_id, kind, account=None, order_id=None):
    return execute_job(sandbox_id, job_id, kind, account, order_id)


_pool = None


def _thread_pool():
    global _pool
    if _pool is None:
        _pool = ThreadPoolExecutor(max_workers=2, thread_name_prefix="email-job")
    return _pool


def _run_in_app(app, *args):
    try:
        with app.app_context():
            execute_job(*args)
    except Exception:
        log.exception("Job thread crashed")


def dispatch(app, sandbox_id, job_id, kind, account=None, order_id=None):
    """Hand a queued job to its executor. Raises if the Celery broker is unreachable."""
    app = getattr(app, "_get_current_object", lambda: app)()  # a real app, not the current_app proxy
    mode = app.config["JOBS_EXECUTION"]
    args = (sandbox_id, job_id, kind, account, order_id)
    if mode == "celery":
        run_email_job.apply_async(
            args, retry=True, retry_policy={"max_retries": 1, "interval_start": 0, "interval_max": 0.2})
    elif mode == "inline":
        _run_in_app(app, *args)
    else:
        _thread_pool().submit(_run_in_app, app, *args)
