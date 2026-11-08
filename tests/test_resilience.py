"""S5 (cache/limiter/counters when Redis is unavailable), N3 (429 body) and S6 (stuck jobs)."""
import os
import subprocess
import sys
import textwrap

import pytest

import applications.caching as caching
from helpers import assert_error, bearer


def run_script(script, tmp_path, **env_over):
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX", "CORS", "JOBS", "REDIS", "CACHE"))}
    env.update(SANDBOX_DIR=str(tmp_path / "sbx"), LOG_LEVEL="ERROR", APP_ENV="development", JOBS_EXECUTION="inline")
    env.update(env_over)
    return subprocess.run([sys.executable, "-c", textwrap.dedent(script)], cwd=root, env=env, capture_output=True,
                          text=True, timeout=180)


# --- N3: 429 body and CORS ---------------------------------------------------------------

def test_429_is_generic_with_retry_after_and_exposed_header(tmp_path):
    result = run_script("""
        import app
        c = app.app.test_client()
        h = {"Origin": "https://frontend.example"}
        rs = [c.post("/api/demo/session", json={"role": "user"}, headers=h) for _ in range(3)]
        r = rs[2]
        assert r.status_code == 429, r.status_code
        body = r.get_json()
        assert body["error"] == "Too many requests. Please try again later.", body
        assert body["code"] == "rate_limited" and 1 <= body["retry_after"] <= 60, body
        assert int(r.headers["Retry-After"]) >= 1
        exposed = r.headers["Access-Control-Expose-Headers"]
        assert "Retry-After" in exposed, exposed
        print("ok")
    """, tmp_path, RATELIMIT_ENABLED="true", DEMO_SESSION_RATE_LIMIT="2 per minute", CORS_ORIGINS="https://frontend.example")
    assert result.returncode == 0 and "ok" in result.stdout, result.stdout + result.stderr


# --- S5: configuration ------------------------------------------------------------------------

def test_limiter_and_cache_are_configured_to_survive_redis_outages(tmp_path):
    result = run_script("""
        import app
        c = app.app.config
        assert c["RATELIMIT_SWALLOW_ERRORS"] is True and c["RATELIMIT_IN_MEMORY_FALLBACK_ENABLED"] is True
        assert c["RATELIMIT_STORAGE_OPTIONS"] == {"socket_connect_timeout": 2, "socket_timeout": 2}
        assert c["CACHE_TYPE"] == "applications.caching.ResilientRedisCache"
        assert c["CACHE_KEY_PREFIX"] == "grocery_cache_"
        assert app.limiter._swallow_errors and app.limiter._in_memory_fallback_enabled
        print("ok")
    """, tmp_path, REDIS_URL="redis://127.0.0.1:1/0", JOBS_EXECUTION="thread")
    assert result.returncode == 0 and "ok" in result.stdout, result.stdout + result.stderr


# --- S5: safe_cache_clear and the resilient cache -----------------------------------------------

def test_safe_cache_clear_swallows_errors(monkeypatch, caplog):
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(caching.cache, "clear", boom)
    assert caching.safe_cache_clear() is False
    assert "Could not clear the response cache" in caplog.text


def test_safe_cache_clear_returns_true_when_it_works():
    assert caching.safe_cache_clear() is True


def test_writes_survive_a_failing_cache_clear(client, admin_token, monkeypatch, query):
    def boom():
        raise ConnectionError("redis down")

    monkeypatch.setattr(caching.cache, "clear", boom)
    body = {"product_name": "Kiwi", "category": "Dairy", "unit": "kg", "quantity": 1, "price": 1,
            "manufacture_date": "2026-01-01", "expiry_date": "2027-01-01"}
    r = client.post("/api/manager/add_product", json=body, headers=bearer(admin_token))
    assert r.status_code == 201
    assert query("SELECT COUNT(*) FROM products WHERE name = 'Kiwi'") == [(1,)]


def test_no_code_calls_cache_clear_directly():
    import glob
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for path in glob.glob(os.path.join(root, "applications", "*.py")):
        if path.endswith("caching.py"):
            continue
        assert "cache.clear()" not in open(path).read().replace("_cache.clear()", ""), path


class DeadRedis:
    """Stands in for a Redis whose every call raises."""

    calls = 0

    def __getattr__(self, name):
        def fail(*a, **k):
            DeadRedis.calls += 1
            raise ConnectionError("down")
        return fail


def test_resilient_cache_degrades_to_misses_and_backs_off(monkeypatch):
    from cachelib import RedisCache as Base
    cache = caching.ResilientRedisCache(host=DeadRedis(), key_prefix="t_")
    DeadRedis.calls = 0
    assert cache.get("k") is None
    first = DeadRedis.calls
    assert first >= 1
    assert cache.set("k", 1) is False and cache.get("k") is None and cache.delete("k") is False and cache.clear() is False
    assert DeadRedis.calls == first  # in the cool-down: Redis is not even tried
    cache._down_until = 0  # cool-down over: tried again
    cache.get("k")
    assert DeadRedis.calls > first


def test_cached_endpoints_work_with_a_dead_cache(app, client, monkeypatch):
    cache = caching.ResilientRedisCache(host=DeadRedis(), key_prefix="t_")
    monkeypatch.setitem(app.extensions["cache"], next(iter(app.extensions["cache"])), cache)
    assert client.get("/api/getCategories").status_code == 200
    assert client.get("/api/user_dashboard").status_code == 200


def test_email_counters_stay_fail_closed(app, client, monkeypatch):
    """A broken counter store must not let e-mails through unlimited (503, not 202)."""
    from applications.email_limits import get_counters

    client.post("/api/demo/email", json={"email": "a@example.com"})

    def boom(key):
        raise ConnectionError("redis down")

    monkeypatch.setattr(get_counters(app), "incr", boom)
    r = client.post("/api/demo/jobs", json={"kind": "daily_reminder"})
    assert r.status_code == 503 and r.get_json()["error"]


# --- S6: stuck jobs and duplicate deliveries ---------------------------------------------------

def _insert_job(execute, status, updated_at, kind="daily_reminder"):
    execute("INSERT INTO jobs (kind, status, to_email, created_at, updated_at) VALUES (?, ?, 'v@example.com', ?, ?)",
            kind, status, updated_at, updated_at)


def test_stale_queued_and_running_jobs_are_failed_when_listed(app, client, execute, query):
    import applications.backend_tasks as tasks
    old, fresh = tasks.now_iso(-600), tasks.now_iso(-10)
    _insert_job(execute, "queued", old)
    _insert_job(execute, "running", old)
    _insert_job(execute, "queued", fresh)
    _insert_job(execute, "sent", old)
    jobs = client.get("/api/demo/jobs").get_json()["jobs"]
    by_id = {j["id"]: j for j in jobs}
    assert [by_id[i]["status"] for i in (1, 2, 3, 4)] == ["failed", "failed", "queued", "sent"]
    assert by_id[1]["detail"] == by_id[2]["detail"] == "Timed out; please try again."
    assert by_id[3]["detail"] is None and by_id[4]["detail"] is None
    assert query("SELECT status FROM jobs ORDER BY id") == [("failed",), ("failed",), ("queued",), ("sent",)]


def test_stale_threshold_is_configurable(app, client, execute, monkeypatch):
    import applications.backend_tasks as tasks
    _insert_job(execute, "queued", tasks.now_iso(-30))
    assert client.get("/api/demo/jobs").get_json()["jobs"][0]["status"] == "queued"
    monkeypatch.setitem(app.config, "JOB_STALE_SECONDS", 10)
    assert client.get("/api/demo/jobs").get_json()["jobs"][0]["status"] == "failed"


def test_default_stale_seconds_is_180(app):
    from applications.config import Config
    assert Config.JOB_STALE_SECONDS == 180


def _run_job(app, sandbox_id, job_id):
    import applications.backend_tasks as tasks
    with app.app_context():
        return tasks.execute_job(sandbox_id, job_id, "daily_reminder")


def test_a_redelivered_task_sends_nothing_twice(app, client, sandbox_id, monkeypatch, query):
    import applications.backend_tasks as tasks
    sent = []
    monkeypatch.setattr(tasks, "send_email", lambda to, subject, html: sent.append(to) or "sent")
    client.post("/api/demo/email", json={"email": "a@example.com"})
    client.post("/api/demo/jobs", json={"kind": "daily_reminder"})       # inline: runs once
    assert sent == ["a@example.com"]
    assert _run_job(app, sandbox_id, 1) == "duplicate"                   # redelivery after success
    assert sent == ["a@example.com"]
    assert query("SELECT COUNT(*) FROM outbox") == [(1,)]


@pytest.mark.parametrize("status", ["running", "failed", "sent", "preview"])
def test_only_queued_jobs_are_run(app, sandbox_id, execute, query, monkeypatch, status):
    import applications.backend_tasks as tasks
    monkeypatch.setattr(tasks, "send_email", lambda *a: pytest.fail("must not send"))
    _insert_job(execute, status, tasks.now_iso())
    assert _run_job(app, sandbox_id, 1) == "duplicate"
    assert query("SELECT status FROM jobs") == [(status,)]


def test_a_job_that_timed_out_is_not_sent_by_a_late_delivery(app, client, sandbox_id, execute, monkeypatch):
    import applications.backend_tasks as tasks
    monkeypatch.setattr(tasks, "send_email", lambda *a: pytest.fail("must not send"))
    _insert_job(execute, "queued", tasks.now_iso(-900))
    client.get("/api/demo/jobs")                                        # marks it failed
    assert _run_job(app, sandbox_id, 1) == "duplicate"


def test_two_concurrent_deliveries_send_once(app, client, sandbox_id, execute, monkeypatch):
    import threading
    import applications.backend_tasks as tasks
    sent = []
    monkeypatch.setattr(tasks, "send_email", lambda to, s, h: sent.append(1) or "sent")
    _insert_job(execute, "queued", tasks.now_iso())
    results = []
    threads = [threading.Thread(target=lambda: results.append(_run_job(app, sandbox_id, 1))) for _ in range(4)]
    [t.start() for t in threads]
    [t.join() for t in threads]
    assert len(sent) == 1 and sorted(results) == ["duplicate"] * 3 + ["sent"]


def test_celery_visibility_timeout_is_five_minutes():
    import celeryconfig
    assert celeryconfig.broker_transport_options["visibility_timeout"] == 300
