"""Visitor e-mail, jobs, outbox, limits, templates and the checkout hook (HTTP calls mocked)."""
import os
import subprocess
import sys
import textwrap
import threading
import time
import urllib.error

import pytest

import applications.backend_tasks as tasks
import applications.jobs_routes as jobs_routes
import applications.mail_service as mail_service
from applications.email_limits import get_counters
from applications.sandbox import bind_sandbox
from helpers import APPLE, assert_error, bearer, mint_token

EMAIL = "Visitor@Example.com"


@pytest.fixture
def set_email(client):
    def run(address=EMAIL, **kwargs):
        response = client.post("/api/demo/email", json={"email": address, **kwargs})
        assert response.status_code == 200, response.get_json()
        return response.get_json()["email"]

    return run


@pytest.fixture
def resend(app, monkeypatch):
    """Resend mode with the HTTPS call recorded (returns the list of calls)."""
    calls = []

    def fake(url, payload, headers=None, timeout=10):
        calls.append({"url": url, "payload": payload, "headers": headers, "timeout": timeout})
        return 200, {"id": "abc"}

    monkeypatch.setitem(app.config, "RESEND_API_KEY", "re_test_key")
    monkeypatch.setattr(mail_service, "http_post_json", fake)
    return calls


def _jobs(client):
    return client.get("/api/demo/jobs").get_json()


def _post_job(client, kind="daily_reminder", **kwargs):
    return client.post("/api/demo/jobs", json={"kind": kind}, **kwargs)


# --- address validation and Turnstile -------------------------------------------------------

def test_email_is_normalised_and_stored(client, query):
    assert client.post("/api/demo/email", json={"email": "  Visitor@Example.COM "}).get_json() == {
        "email": "visitor@example.com"}
    assert _jobs(client)["email"] == "visitor@example.com"
    assert query("SELECT value FROM meta WHERE key = 'visitor_email'") == [("visitor@example.com",)]


@pytest.mark.parametrize("bad", ["", "nope", "a@b", "a@b.c", "@x.com", "a b@x.com", "a@@x.com", "a@x..com",
                                 ".a@x.com", "a.@x.com", "a@-x.com", "x@y.com\r\nBcc: z@z.com", "a" * 250 + "@x.com",
                                 "a" * 65 + "@x.com", None, 5, ["a@x.com"]])
def test_invalid_addresses_are_rejected(client, bad):
    assert_error(client.post("/api/demo/email", json={"email": bad}), 400)
    assert _jobs(client)["email"] is None


def test_email_endpoints_need_a_sandbox(app):
    anon = app.test_client()
    anon.sandbox_id = None
    for response in (anon.post("/api/demo/email", json={"email": "a@x.com"}), anon.get("/api/demo/jobs"),
                     anon.get("/api/demo/outbox"), anon.post("/api/demo/jobs", json={"kind": "daily_reminder"})):
        assert response.status_code == 410 and response.get_json()["code"] == "sandbox_expired"


@pytest.fixture
def turnstile(app, monkeypatch):
    calls = []
    result = {"status": 200, "body": {"success": True}}

    def fake(url, payload, headers=None, timeout=10):
        calls.append((url, payload, timeout))
        if isinstance(result["body"], Exception):
            raise result["body"]
        return result["status"], result["body"]

    monkeypatch.setitem(app.config, "TURNSTILE_SECRET_KEY", "ts-secret")
    monkeypatch.setattr(jobs_routes, "http_post_json", fake)
    return calls, result


def test_turnstile_passes(client, turnstile):
    calls, _ = turnstile
    response = client.post("/api/demo/email", json={"email": "a@x.com", "turnstile_token": "tok"})
    assert response.status_code == 200
    url, payload, timeout = calls[0]
    assert url == "https://challenges.cloudflare.com/turnstile/v0/siteverify"
    assert payload["secret"] == "ts-secret" and payload["response"] == "tok" and payload["remoteip"] == "127.0.0.1"
    assert timeout


def test_turnstile_failure_is_400_and_stores_nothing(client, turnstile):
    calls, result = turnstile
    result["body"] = {"success": False, "error-codes": ["invalid-input-response"]}
    assert_error(client.post("/api/demo/email", json={"email": "a@x.com", "turnstile_token": "bad"}), 400)
    assert _jobs(client)["email"] is None


def test_turnstile_token_required_when_enabled(client, turnstile):
    calls, _ = turnstile
    assert_error(client.post("/api/demo/email", json={"email": "a@x.com"}), 400)
    assert_error(client.post("/api/demo/email", json={"email": "a@x.com", "turnstile_token": ""}), 400)
    assert calls == []


def test_turnstile_outage_fails_closed(client, turnstile):
    _, result = turnstile
    result["body"] = urllib.error.URLError("down")
    assert_error(client.post("/api/demo/email", json={"email": "a@x.com", "turnstile_token": "t"}), 503)


def test_turnstile_disabled_without_secret(client, monkeypatch):
    def boom(*a, **k):
        raise AssertionError("must not be called")

    monkeypatch.setattr(jobs_routes, "http_post_json", boom)
    assert client.post("/api/demo/email", json={"email": "a@x.com", "turnstile_token": "ignored"}).status_code == 200


# --- jobs: lifecycle, preview and Resend --------------------------------------------------

def test_job_needs_an_email_and_a_known_kind(client, set_email):
    assert_error(_post_job(client), 400)  # no address yet
    set_email()
    assert_error(_post_job(client, "order_confirmation"), 400)  # not user-requestable
    assert_error(client.post("/api/demo/jobs", json={"kind": "spam_everyone"}), 400)
    assert _jobs(client)["jobs"] == []


def test_preview_mode_stores_the_message_and_delivers_nothing(client, set_email, monkeypatch):
    monkeypatch.setattr(mail_service, "http_post_json", lambda *a, **k: pytest.fail("no HTTP in preview mode"))
    set_email()
    body = _jobs(client)
    assert body["email_enabled"] is False
    response = _post_job(client)
    assert response.status_code == 202
    job = response.get_json()["job"]
    assert job["status"] == "queued" and job["kind"] == "daily_reminder" and job["to_email"] == "visitor@example.com"
    final = _jobs(client)["jobs"][0]
    assert final["id"] == job["id"] and final["status"] == "preview" and final["detail"]
    messages = client.get("/api/demo/outbox").get_json()["messages"]
    assert len(messages) == 1 and messages[0]["job_id"] == job["id"] and messages[0]["to_email"] == "visitor@example.com"
    assert "Knock knock" in messages[0]["subject"] and "Apple" in messages[0]["html"]


def test_resend_payload_and_sent_status(client, set_email, resend):
    set_email()
    assert _jobs(client)["email_enabled"] is True
    _post_job(client, "daily_reminder")
    assert len(resend) == 1
    call = resend[0]
    assert call["url"] == "https://api.resend.com/emails"
    assert call["headers"]["Authorization"] == "Bearer re_test_key"
    payload = call["payload"]
    assert payload["from"] == "Grocery Store Demo <demo@mail.driptobhattacharyya.com>"
    assert payload["to"] == ["visitor@example.com"]
    assert payload["subject"] == "Knock knock: your Grocery Store demo reminder"
    assert "Hey, we've been missing you" in payload["html"]
    assert "sent once, on request. No mailing list." in payload["html"]
    job = _jobs(client)["jobs"][0]
    assert job["status"] == "sent"
    assert client.get("/api/demo/outbox").get_json()["messages"][0]["html"] == payload["html"]


def test_custom_from_address(app, client, set_email, resend, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_FROM", "Shop <shop@example.org>")
    set_email()
    _post_job(client)
    assert resend[0]["payload"]["from"] == "Shop <shop@example.org>"


def test_provider_error_fails_the_job_with_a_clear_detail(app, client, set_email, monkeypatch):
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "k")
    monkeypatch.setattr(mail_service, "http_post_json", lambda *a, **k: (403, {"message": "domain not verified"}))
    set_email()
    _post_job(client)
    job = _jobs(client)["jobs"][0]
    assert job["status"] == "failed" and "HTTP 403" in job["detail"] and "domain not verified" in job["detail"]
    assert client.get("/api/demo/outbox").get_json()["messages"] == []


def test_network_error_fails_the_job(app, client, set_email, monkeypatch):
    def down(*a, **k):
        raise urllib.error.URLError("secret internal detail")

    monkeypatch.setitem(app.config, "RESEND_API_KEY", "k")
    monkeypatch.setattr(mail_service, "http_post_json", down)
    set_email()
    _post_job(client)
    job = _jobs(client)["jobs"][0]
    assert job["status"] == "failed" and "Could not reach the e-mail provider" in job["detail"]
    assert "secret internal detail" not in job["detail"]


def test_kill_switch(app, client, set_email, resend, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_ENABLED", False)
    set_email()
    _post_job(client)
    job = _jobs(client)["jobs"][0]
    assert job["status"] == "failed" and job["detail"] == "e-mail disabled"
    assert resend == []
    assert _jobs(client)["email_enabled"] is False


def test_queued_running_then_final_status_on_a_thread(app, client, set_email, monkeypatch):
    """No-Redis production mode: the job really is queued, then running, then done."""
    gate, started = threading.Event(), threading.Event()
    real = tasks.send_email

    def slow(*args):
        started.set()
        assert gate.wait(10)
        return real(*args)

    monkeypatch.setitem(app.config, "JOBS_EXECUTION", "thread")
    monkeypatch.setattr(tasks, "send_email", slow)
    set_email()
    assert _post_job(client).get_json()["job"]["status"] == "queued"
    assert started.wait(10)
    assert _jobs(client)["jobs"][0]["status"] == "running"
    gate.set()
    for _ in range(100):
        if _jobs(client)["jobs"][0]["status"] == "preview":
            break
        time.sleep(0.05)
    assert _jobs(client)["jobs"][0]["status"] == "preview"
    assert _jobs(client)["jobs"][0]["updated_at"] >= _jobs(client)["jobs"][0]["created_at"]


def test_counter_store_outage_is_a_503_not_a_500(app, client, set_email, monkeypatch):
    """Redis down: POST /api/demo/jobs says the queue is unavailable (JSON 503) and leaves no stuck job."""
    set_email()

    class Down:
        def incr(self, key):
            raise ConnectionError("redis is down")

    monkeypatch.setitem(app.extensions, "email_counters", Down())
    assert_error(client.post("/api/demo/jobs", json={"kind": "monthly_report"}), 503)
    assert _jobs(client)["jobs"] == []


def test_rate_limiter_fails_open_when_its_store_errors(app):
    import sys
    assert sys.modules[app.import_name].limiter._swallow_errors is True


def test_jobs_and_outbox_are_listed_newest_first(client, set_email):
    set_email()
    _post_job(client, "daily_reminder")
    _post_job(client, "monthly_report")
    jobs = _jobs(client)["jobs"]
    assert [j["kind"] for j in jobs] == ["monthly_report", "daily_reminder"]
    messages = client.get("/api/demo/outbox").get_json()["messages"]
    assert [m["job_id"] for m in messages] == [jobs[0]["id"], jobs[1]["id"]]


# --- templates ---------------------------------------------------------------------------------

def test_monthly_report_defaults_to_the_demo_shopper(client, set_email, shopper_token):
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 2}, headers=bearer(shopper_token))
    client.post("/api/shopping_cart/checkout", headers=bearer(shopper_token))
    set_email()
    _post_job(client, "monthly_report")  # anonymous: reports on demo@grocery.app
    html = client.get("/api/demo/outbox").get_json()["messages"][0]["html"]
    assert "demo@grocery.app" in html and "360.00 INR" in html


def test_monthly_report_uses_the_jwt_identity(client, set_email, manager_token):
    set_email()
    _post_job(client, "monthly_report", headers=bearer(manager_token))
    html = client.get("/api/demo/outbox").get_json()["messages"][0]["html"]
    assert "manager@grocery.app" in html and "No orders in the last 30 days" in html


def test_bad_token_on_job_request_is_401(client, set_email):
    set_email()
    assert _post_job(client, headers=bearer("garbage")).status_code == 401


def test_product_names_are_escaped_in_the_outbox(client, set_email, execute):
    execute("UPDATE products SET name = ? WHERE productID = 1", '<script>alert("x")</script>')
    set_email()
    _post_job(client, "daily_reminder")
    html = client.get("/api/demo/outbox").get_json()["messages"][0]["html"]
    assert "<script>" not in html and "&lt;script&gt;" in html


def test_order_confirmation_has_footer_and_escaped_items(client, set_email, execute, shopper_token):
    execute("UPDATE products SET name = ? WHERE productID = ?", "<b>Apple</b>", APPLE)
    set_email()
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1}, headers=bearer(shopper_token))
    order = client.post("/api/shopping_cart/checkout", headers=bearer(shopper_token)).get_json()
    message = client.get("/api/demo/outbox").get_json()["messages"][0]
    html = message["html"]
    assert f"#{order['order_id']}" in message["subject"] and f"#{order['order_id']}" in html
    assert "&lt;b&gt;Apple&lt;/b&gt;" in html and "<b>Apple</b>" not in html
    assert "180.00 INR" in html
    assert "grocery-store.driptobhattacharyya.com" in html and "No mailing list." in html


# --- checkout hook -----------------------------------------------------------------------------

def test_checkout_enqueues_confirmation_only_with_an_email(client, set_email, shopper_token):
    def checkout():
        client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1}, headers=bearer(shopper_token))
        return client.post("/api/shopping_cart/checkout", headers=bearer(shopper_token))

    assert checkout().status_code == 200
    assert _jobs(client)["jobs"] == []
    set_email()
    assert checkout().status_code == 200
    jobs = _jobs(client)["jobs"]
    assert [(j["kind"], j["status"]) for j in jobs] == [("order_confirmation", "preview")]


def test_checkout_survives_an_enqueue_failure(app, client, set_email, shopper_token, monkeypatch, query):
    def boom(*a, **k):
        raise RuntimeError("broker down")

    monkeypatch.setattr(tasks, "dispatch", boom)
    set_email()
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1}, headers=bearer(shopper_token))
    response = client.post("/api/shopping_cart/checkout", headers=bearer(shopper_token))
    assert response.status_code == 200 and response.get_json()["order_id"]
    assert query("SELECT status FROM jobs") == [("failed",)]


def test_checkout_survives_the_email_limit(app, client, set_email, shopper_token, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_RECIPIENT_DAILY_LIMIT", 0)
    set_email()
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1}, headers=bearer(shopper_token))
    assert client.post("/api/shopping_cart/checkout", headers=bearer(shopper_token)).status_code == 200
    assert _jobs(client)["jobs"] == []


# --- limits ---------------------------------------------------------------------------------------

def test_per_recipient_limit_is_5_and_spans_sandboxes(app, client, set_email, sandbox_client, new_sandbox):
    set_email()
    assert [_post_job(client).status_code for _ in range(5)] == [202] * 5
    over = _post_job(client)
    assert over.status_code == 429 and over.get_json()["code"] == "email_limit" and over.get_json()["error"]
    assert len(_jobs(client)["jobs"]) == 5  # the refused request created no job
    # A brand-new sandbox with the same address is still blocked.
    other = sandbox_client(new_sandbox())
    other.post("/api/demo/email", json={"email": "VISITOR@example.com"})
    assert other.post("/api/demo/jobs", json={"kind": "daily_reminder"}).status_code == 429


def test_per_ip_limit_is_10_across_addresses_and_sandboxes(app, sandbox_client, new_sandbox):
    results = []
    for i in range(11):
        c = sandbox_client(new_sandbox())
        c.post("/api/demo/email", json={"email": f"person{i}@example.com"})
        results.append(c.post("/api/demo/jobs", json={"kind": "daily_reminder"}))
    assert [r.status_code for r in results] == [202] * 10 + [429]
    assert results[-1].get_json()["code"] == "email_limit"


def test_other_ips_are_not_affected(app, sandbox_client, new_sandbox):
    for i in range(10):
        c = sandbox_client(new_sandbox())
        c.post("/api/demo/email", json={"email": f"p{i}@example.com"})
        c.post("/api/demo/jobs", json={"kind": "daily_reminder"})
    c = sandbox_client(new_sandbox())
    c.post("/api/demo/email", json={"email": "late@example.com"})
    ok = c.post("/api/demo/jobs", json={"kind": "daily_reminder"}, environ_overrides={"REMOTE_ADDR": "203.0.113.9"})
    assert ok.status_code == 202


def test_global_cap_counts_real_sends_only(app, sandbox_client, new_sandbox, resend, monkeypatch):
    monkeypatch.setitem(app.config, "EMAIL_DAILY_CAP", 3)
    monkeypatch.setitem(app.config, "EMAIL_IP_DAILY_LIMIT", 100)
    codes = []
    for i in range(5):
        c = sandbox_client(new_sandbox())
        c.post("/api/demo/email", json={"email": f"g{i}@example.com"})
        codes.append(c.post("/api/demo/jobs", json={"kind": "daily_reminder"}).status_code)
    assert codes == [202, 202, 202, 429, 429]
    # Preview mode (no provider) does not use the global cap.
    monkeypatch.setitem(app.config, "RESEND_API_KEY", None)
    c = sandbox_client(new_sandbox())
    c.post("/api/demo/email", json={"email": "preview@example.com"})
    assert c.post("/api/demo/jobs", json={"kind": "daily_reminder"}).status_code == 202


def test_failed_enqueue_gives_the_slot_back(app, client, set_email, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("down")

    set_email()
    monkeypatch.setattr(tasks, "dispatch", boom)
    for _ in range(6):
        assert _post_job(client).status_code == 503
    monkeypatch.undo()
    assert _post_job(client).status_code == 202


def test_limits_hold_in_a_fresh_process_with_rate_limiting(tmp_path):
    script = textwrap.dedent("""
        import app
        c = app.app.test_client()
        h = {"X-Sandbox-Id": c.post("/api/demo/session", json={"role": "user"}).get_json()["sandbox_id"]}
        codes = [c.post("/api/demo/email", json={"email": "a@example.com"}, headers=h).status_code for _ in range(4)]
        assert codes == [200, 200, 200, 429], codes
        assert c.get("/api/demo/jobs", headers=h).status_code == 200
        jobs = [c.post("/api/demo/jobs", json={"kind": "daily_reminder"}, headers=h).status_code for _ in range(3)]
        assert jobs == [429, 429, 429], jobs  # DEMO_JOBS_RATE_LIMIT=0 would be odd; see below
        print("ok")
    """).replace("[429, 429, 429], jobs  # DEMO_JOBS_RATE_LIMIT=0 would be odd; see below", "[202, 202, 429], jobs")
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX", "CORS", "JOBS"))}
    env.update(RATELIMIT_ENABLED="true", DEMO_EMAIL_RATE_LIMIT="3 per hour", DEMO_JOBS_RATE_LIMIT="2 per hour",
               SANDBOX_DIR=str(tmp_path / "sbx"), LOG_LEVEL="ERROR", APP_ENV="development", JOBS_EXECUTION="inline")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr


# --- expired sandboxes, old tasks, supervisor -----------------------------------------------------

def test_task_for_an_expired_sandbox_does_nothing(app, store, caplog):
    sid = store.create()
    store.delete(sid)
    with app.app_context():
        assert tasks.execute_job(sid, 1, "daily_reminder") == "expired"
    with app.app_context():
        assert tasks.execute_job("not-a-sandbox-id", 1, "daily_reminder") == "expired"


def test_task_for_an_unknown_job_is_skipped(app, sandbox_id):
    with app.app_context():
        assert tasks.execute_job(sandbox_id, 999, "daily_reminder") == "missing"


def test_celery_task_runs_the_job_in_an_app_context(app, client, set_email, sandbox_id, execute, query):
    """What the worker does: call the registered task with ids only."""
    set_email()
    execute("INSERT INTO jobs (kind, status, to_email, created_at, updated_at) "
            "VALUES ('daily_reminder', 'queued', 'visitor@example.com', 'x', 'x')")
    from app import celery_app

    task = celery_app.tasks["grocery.run_email_job"]
    task(sandbox_id, 1, "daily_reminder")  # FlaskTask.__call__ supplies the app context
    assert query("SELECT status FROM jobs") == [("preview",)]
    assert celery_app.conf.beat_schedule == {}


def test_send_to_everyone_tasks_are_gone():
    assert not hasattr(tasks, "daily_reminder") and not hasattr(tasks, "monthly_report")
    assert not hasattr(mail_service, "send_message")
    from app import celery_app

    assert not [name for name in celery_app.tasks if name.startswith("applications.")]


def test_smtp_is_opt_in_only(app, monkeypatch):
    monkeypatch.setitem(app.config, "RESEND_API_KEY", None)
    assert mail_service.backend_name(app.config) == "preview"
    monkeypatch.setitem(app.config, "EMAIL_BACKEND", "smtp")
    assert mail_service.backend_name(app.config) == "smtp"
    monkeypatch.setitem(app.config, "EMAIL_BACKEND", "")
    monkeypatch.setitem(app.config, "RESEND_API_KEY", "k")
    assert mail_service.backend_name(app.config) == "resend"


def test_passive_store_never_deletes_files(app, tmp_path):
    from applications.sandbox import SandboxStore

    template = tmp_path / "t.db"
    store = SandboxStore(str(tmp_path / "s"), str(template), ttl_seconds=60, max_count=1, passive=True)
    sid = "a" * 32
    path = store.path_for(sid)
    open(path, "w").close()
    old = time.time() - 3600
    os.utime(path, (old, old))
    assert store.touch(sid) is None  # expired by mtime, but the file stays: the web process owns deletion
    assert os.path.exists(path)
    os.utime(path, None)
    assert store.touch(sid) == path
