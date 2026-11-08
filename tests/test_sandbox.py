"""Sandbox lifecycle (registry, expiry, eviction, ids), 410 handling, cache and
data isolation between sandboxes, and CORS."""
import os
import sqlite3
import threading
import time

import pytest

from applications.sandbox import SandboxStore, is_valid_id
from helpers import ADMIN, APPLE, DEMO_USER, assert_error, bearer

ORIGIN = "https://frontend.example"


# --- ids -------------------------------------------------------------------------

def test_sandbox_ids_are_unguessable_and_well_formed(store, new_sandbox):
    ids = {new_sandbox() for _ in range(5)}
    assert len(ids) == 5
    assert all(is_valid_id(i) and len(i) == 32 for i in ids)


@pytest.mark.parametrize("bad", ["", "short", "../../etc/passwd", "a" * 31, "a" * 33, "a" * 31 + "/",
                                 "a" * 30 + "..", "a" * 31 + "\n", "a" * 31 + "\x00", "ä" * 32, None, 5])
def test_malformed_ids_are_rejected(store, bad):
    assert not is_valid_id(bad)
    with pytest.raises(ValueError):
        store.path_for(bad)
    assert store.touch(bad) is None


def test_sandbox_file_lives_inside_the_sandbox_dir(store, sandbox_id):
    path = store.path_for(sandbox_id)
    assert os.path.dirname(path) == store.directory and os.path.isfile(path)


# --- 410 for missing / unknown / expired -------------------------------------------

SANDBOXED = [
    ("get", "/api/user_dashboard"), ("get", "/api/getCategories"), ("get", "/api/category/1"),
    ("post", "/api/search"), ("post", "/api/login"), ("post", "/api/signup"), ("post", "/api/admin/login"),
    ("get", "/api/shopping_cart"), ("post", "/api/add_to_cart"), ("get", "/api/dashboard"),
    ("get", "/api/manager/dashboard"), ("get", "/api/admin/manager_requests"),
    ("post", "/api/demo/switch"), ("get", "/api/demo/db"), ("post", "/api/demo/sql"),
]


def _assert_gone(response):
    assert response.status_code == 410, response.get_data(as_text=True)
    body = response.get_json()
    assert body["code"] == "sandbox_expired" and isinstance(body["error"], str) and body["error"]


@pytest.mark.parametrize("method,path", SANDBOXED)
def test_missing_sandbox_header_is_410(client, method, path):
    client.sandbox_id = None
    _assert_gone(getattr(client, method)(path, json={}))


@pytest.mark.parametrize("method,path", SANDBOXED)
def test_unknown_sandbox_is_410(client, method, path):
    _assert_gone(getattr(client, method)(path, json={}, headers={"X-Sandbox-Id": "A" * 32}))


@pytest.mark.parametrize("value", ["../../etc/passwd", "x", "A" * 40, "a/../" + "b" * 27, "%2e%2e" * 6])
def test_malformed_sandbox_header_is_410(client, value):
    _assert_gone(client.get("/api/user_dashboard", headers={"X-Sandbox-Id": value}))


def test_410_beats_401(client, sandbox_id, store):
    store.delete(sandbox_id)
    _assert_gone(client.get("/api/shopping_cart"))  # no token either


def test_unsandboxed_endpoints_need_no_header(client):
    client.sandbox_id = None
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/ready").status_code == 200
    assert client.post("/api/demo/session", json={"role": "user"}).status_code == 201
    # Unknown routes stay plain 404s rather than 410s.
    assert_error(client.get("/api/nope"), 404)
    assert_error(client.get("/api/login"), 405)


# --- expiry, refresh, eviction --------------------------------------------------------

@pytest.fixture
def small_store(app, tmp_path):
    template = tmp_path / "template.db"
    import shutil

    shutil.copy(app.extensions["sandbox_store"].template_path, template)
    return SandboxStore(str(tmp_path / "sbx"), str(template), ttl_seconds=60, max_count=3)


def _age(store, sandbox_id, seconds):
    with store._lock:
        store._last_used[sandbox_id] -= seconds


def test_idle_sandbox_expires_and_its_files_are_removed(small_store):
    sid = small_store.create()
    path = small_store.path_for(sid)
    assert small_store.touch(sid) == path
    _age(small_store, sid, 61)
    assert small_store.touch(sid) is None
    assert not os.path.exists(path) and small_store.count() == 0


def test_every_touch_refreshes_the_idle_timer(small_store):
    sid = small_store.create()
    _age(small_store, sid, 50)
    assert small_store.touch(sid)
    _age(small_store, sid, 50)  # 50s since the last touch, not 100s since creation
    assert small_store.touch(sid)


def test_create_evicts_the_least_recently_used_when_full(small_store):
    a, b, c = (small_store.create() for _ in range(3))
    small_store.touch(a)  # b is now the least recently used
    d = small_store.create()
    assert small_store.count() == 3
    assert small_store.touch(b) is None and not os.path.exists(small_store.path_for(b))
    assert all(small_store.touch(i) for i in (a, c, d))


def test_create_drops_expired_sandboxes_before_evicting(small_store):
    a, b, c = (small_store.create() for _ in range(3))
    _age(small_store, b, 120)
    d = small_store.create()
    assert small_store.touch(b) is None
    assert all(small_store.touch(i) for i in (a, c, d))


def test_startup_adopts_fresh_files_and_deletes_stale_ones(small_store):
    fresh, stale = small_store.create(), small_store.create()
    old = time.time() - 3600
    os.utime(small_store.path_for(stale), (old, old))
    again = SandboxStore(small_store.directory, small_store.template_path, 60, 3)
    assert again.ids() == [fresh]
    assert not os.path.exists(small_store.path_for(stale))
    assert again.touch(fresh)


def test_unregistered_file_on_disk_is_adopted_on_touch(small_store):
    sid = small_store.create()
    other = SandboxStore.__new__(SandboxStore)  # a second process' view: empty registry
    other.__dict__.update(small_store.__dict__)
    other._last_used = type(small_store._last_used)()
    other._lock = threading.RLock()
    assert other.touch(sid) == small_store.path_for(sid)


def test_concurrent_creates_respect_the_cap(small_store):
    ids = []
    threads = [threading.Thread(target=lambda: ids.append(small_store.create())) for _ in range(12)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(set(ids)) == 12 and small_store.count() == 3
    files = [f for f in os.listdir(small_store.directory) if f.endswith(".db")]
    assert len(files) == 3


def test_sandbox_file_is_self_contained(small_store):
    # No -wal/-shm files are needed to read a freshly created sandbox.
    sid = small_store.create()
    path = small_store.path_for(sid)
    assert not os.path.exists(path + "-wal")
    conn = sqlite3.connect(path)
    assert conn.execute("SELECT COUNT(*) FROM products").fetchone()[0] == 14
    conn.close()


def test_expired_sandbox_via_http_is_410(client, store, sandbox_id):
    assert client.get("/api/user_dashboard").status_code == 200
    _age(store, sandbox_id, store.ttl + 1)
    _assert_gone(client.get("/api/user_dashboard"))
    assert not os.path.exists(store.path_for(sandbox_id))


def test_requests_keep_the_sandbox_alive(client, store, sandbox_id):
    _age(store, sandbox_id, store.ttl - 5)
    assert client.get("/api/user_dashboard").status_code == 200
    _age(store, sandbox_id, store.ttl - 5)
    assert client.get("/api/user_dashboard").status_code == 200


def test_sandbox_deleted_mid_request_is_410_not_a_new_empty_db(app, client, store, sandbox_id, monkeypatch):
    import applications.controllers as controllers

    real = controllers.get_10_products

    def delete_then_query():
        store.delete(sandbox_id)
        return real()

    monkeypatch.setattr(controllers, "get_10_products", delete_then_query)
    _assert_gone(client.get("/api/user_dashboard"))
    assert not os.path.exists(store.path_for(sandbox_id))


# --- isolation --------------------------------------------------------------------------

def test_writes_in_one_sandbox_are_invisible_in_another(client, sandbox_client, new_sandbox, signup, login, query):
    other = sandbox_client(new_sandbox())
    signup("only-here@example.com")
    assert query("SELECT COUNT(*) FROM users WHERE email = 'only-here@example.com'") == [(1,)]
    response = other.post("/api/login", json={"email": "only-here@example.com", "password": "secret123"})
    assert_error(response, 401)
    token = login(*DEMO_USER)
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 2}, headers=bearer(token))
    other_token = other.post("/api/login", json={"email": DEMO_USER[0], "password": DEMO_USER[1]}).get_json()["access_token"]
    assert other.get("/api/shopping_cart", headers=bearer(other_token)).get_json() == {"cart": []}


def test_cached_responses_are_not_shared_between_sandboxes(client, sandbox_client, new_sandbox, store):
    other_id = new_sandbox()
    other = sandbox_client(other_id)

    def rename(sid, name):  # direct write: nothing clears the response cache
        conn = sqlite3.connect(store.path_for(sid))
        conn.execute("UPDATE categories SET category_name = ? WHERE categoryID = 1", (name,))
        conn.execute("UPDATE products SET name = ? WHERE productID = 1", (name + " item",))
        conn.commit()
        conn.close()

    # Sandbox A's responses get cached ...
    a_categories = client.get("/api/getCategories").get_json()
    a_dashboard = client.get("/api/user_dashboard").get_json()
    # ... then B's data differs. With a path-only key B would be served A's cached copy.
    rename(other_id, "Only in B")
    b_categories = other.get("/api/getCategories").get_json()
    b_dashboard = other.get("/api/user_dashboard").get_json()
    assert "Only in B" in str(b_categories) and "Only in B item" in str(b_dashboard)
    # And A is still served its own cached copy, not B's.
    assert client.get("/api/getCategories").get_json() == a_categories
    assert client.get("/api/user_dashboard").get_json() == a_dashboard
    assert "Only in B" not in str(a_categories) + str(a_dashboard)


def test_per_user_cached_responses_are_not_shared_between_sandboxes(client, sandbox_client, new_sandbox, login):
    other = sandbox_client(new_sandbox())
    token_a = login(*DEMO_USER)
    token_b = other.post("/api/login", json={"email": DEMO_USER[0], "password": DEMO_USER[1]}).get_json()["access_token"]
    assert client.get("/api/shopping_cart", headers=bearer(token_a)).get_json() == {"cart": []}  # primes A's cache
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1}, headers=bearer(token_a))
    assert len(client.get("/api/shopping_cart", headers=bearer(token_a)).get_json()["cart"]) == 1
    assert other.get("/api/shopping_cart", headers=bearer(token_b)).get_json() == {"cart": []}


def test_cache_keys_include_the_sandbox_id(app, client, sandbox_id):
    from applications.auth import public_cache_key

    with app.test_request_context("/api/getCategories"):
        from flask import g

        g.sandbox_id = sandbox_id
        key_a = public_cache_key()
        g.sandbox_id = "B" * 32
        assert key_a != public_cache_key() and sandbox_id in key_a


# --- CORS ----------------------------------------------------------------------------------

def test_preflight_allows_the_demo_headers(client):
    client.sandbox_id = None
    response = client.options("/api/demo/sql", headers={
        "Origin": ORIGIN, "Access-Control-Request-Method": "POST",
        "Access-Control-Request-Headers": "authorization,content-type,x-sandbox-id"})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    allowed = response.headers["Access-Control-Allow-Headers"].lower()
    assert all(h in allowed for h in ("authorization", "content-type", "x-sandbox-id"))
    assert "POST" in response.headers["Access-Control-Allow-Methods"]


def test_actual_responses_carry_cors_and_expose_content_disposition(client, manager_token):
    response = client.get("/api/manager/prepare_report", headers={**bearer(manager_token), "Origin": ORIGIN})
    assert response.status_code == 200
    assert response.headers["Access-Control-Allow-Origin"] == ORIGIN
    assert "Content-Disposition" in response.headers["Access-Control-Expose-Headers"]


def test_other_origins_get_no_cors_headers(client):
    response = client.get("/api/user_dashboard", headers={"Origin": "https://evil.example"})
    assert response.status_code == 200 and "Access-Control-Allow-Origin" not in response.headers


def test_error_responses_carry_cors_headers(app, client, monkeypatch, sandbox_id):
    h = {"Origin": ORIGIN}
    r401 = client.get("/api/shopping_cart", headers=h)
    r404 = client.get("/api/nope", headers=h)
    r400 = client.post("/api/login", json={}, headers=h)
    r410 = client.get("/api/user_dashboard", headers={**h, "X-Sandbox-Id": "A" * 32})
    r410_missing = client.get("/api/user_dashboard", headers={"Origin": ORIGIN, "X-Sandbox-Id": ""})
    assert [r.status_code for r in (r401, r404, r400, r410, r410_missing)] == [401, 404, 400, 410, 410]

    import applications.controllers as controllers
    monkeypatch.setattr(controllers, "get_10_products", lambda: 1 / 0)
    r500 = client.get("/api/user_dashboard", headers=h)  # handled by api_errors
    monkeypatch.setitem(app.view_functions, "health", lambda: 1 / 0)
    r500b = client.get("/api/health", headers=h)  # handled by the app-level handler
    assert r500.status_code == 500 and r500b.status_code == 500
    for response in (r401, r404, r400, r410, r410_missing, r500, r500b):
        assert response.headers.get("Access-Control-Allow-Origin") == ORIGIN, response.status_code


def test_429_carries_cors_headers(tmp_path):
    import subprocess
    import sys
    import textwrap

    script = textwrap.dedent("""
        import app
        c = app.app.test_client()
        h = {"Origin": "https://frontend.example"}
        statuses = [c.post("/api/demo/session", json={"role": "user"}, headers=h) for _ in range(3)]
        print([r.status_code for r in statuses])
        assert [r.status_code for r in statuses] == [201, 201, 429]
        assert statuses[2].headers["Access-Control-Allow-Origin"] == "https://frontend.example"
        assert statuses[2].get_json()["error"]
    """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX"))}
    env.update(RATELIMIT_ENABLED="true", DEMO_SESSION_RATE_LIMIT="2 per minute", CORS_ORIGINS="https://frontend.example",
               SANDBOX_DIR=str(tmp_path / "sbx"), LOG_LEVEL="ERROR", APP_ENV="development")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
