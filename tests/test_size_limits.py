"""Response-size bounds (B1), sandbox/field size caps (B2) and the SQL console limits (N1, N2)."""
import json
import os

import pytest

import applications.demo_routes as demo_routes
from helpers import assert_error, bearer


def sql(client, text):
    return client.post("/api/demo/sql", json={"sql": text})


# --- B1: SQL console ----------------------------------------------------------------------

def test_sql_never_builds_values_over_100kb(client):
    # the reviewer's attack: 900 KB per cell x 201 rows. SQLite's printf() yields NULL
    # beyond the limit; other builders (zeroblob, hex, replace) raise.
    r = sql(client, "select printf('%.*c',900000,'a') from (with recursive c(x) as "
                    "(select 1 union all select x+1 from c limit 201) select x from c)")
    assert r.status_code == 200 and len(r.data) < 10_000
    assert all(row == [None] for row in r.get_json()["rows"])
    for big in ("zeroblob(200000)", "hex(zeroblob(60000))", "replace(hex(zeroblob(40000)),'0','00')"):
        r = sql(client, f"select {big}")
        assert r.status_code == 400 and "too large" in r.get_json()["error"], big


def test_sql_cells_are_cut_to_2kb_and_the_total_to_256kb(client):
    r = sql(client, "select printf('%.*c',90000,'a') from (with recursive c(x) as "
                    "(select 1 union all select x+1 from c limit 201) select x from c)")
    assert r.status_code == 200
    body = r.get_json()
    assert body["truncated"] is True
    cells = [row[0] for row in body["rows"]]
    assert all(len(c) == demo_routes.MAX_CELL_CHARS + len(demo_routes.TRUNCATED_MARK) for c in cells)
    assert cells[0].endswith("(truncated)")
    assert len(r.data) < 300 * 1024
    assert 100 <= len(cells) < 200  # stopped by the byte budget, not the 200-row cap


def test_sql_small_results_are_untouched(client):
    body = sql(client, "select email from users").get_json()
    assert body["truncated"] is False and body["rows"][0][0].endswith("@grocery.app")


def test_sql_time_limit_is_half_a_second(client):
    assert demo_routes.SQL_TIME_LIMIT == 0.5
    r = sql(client, "with recursive c(x) as (select 1 union all select x+1 from c) select count(*) from c")
    assert_error(r, 400)
    assert "0.5" in r.get_json()["error"]


def test_sql_rate_limit_default_is_20_per_minute(app):
    assert app.config["DEMO_SQL_RATE_LIMIT"] == "20 per minute"


def test_sql_on_an_evicted_sandbox_is_410_not_500(app, client, sandbox_path):
    # the file vanishes after the request passed the sandbox check
    os.remove(sandbox_path)
    for suffix in ("-wal", "-shm"):
        if os.path.exists(sandbox_path + suffix):
            os.remove(sandbox_path + suffix)
    from applications.sandbox import get_store
    store = get_store(app)
    real = store.touch
    store.touch = lambda sid: sandbox_path  # skip the registry check: simulate the race
    try:
        r = sql(client, "select 1")
    finally:
        store.touch = real
    assert r.status_code == 410 and r.get_json()["code"] == "sandbox_expired"


def test_db_snapshot_is_bounded(client, execute):
    big = "x" * 5000
    for i in range(60):
        execute("INSERT INTO categories (category_name) VALUES (?)", big + str(i))
    r = client.get("/api/demo/db")
    assert r.status_code == 200
    table = next(t for t in r.get_json()["tables"] if t["name"] == "categories")
    assert table["truncated"] is True
    assert table["row_count"] == 60 + 5
    assert all(len(row[1]) <= demo_routes.MAX_CELL_CHARS + len(demo_routes.TRUNCATED_MARK) for row in table["rows"])
    assert len(r.data) < 14 * demo_routes.DB_TABLE_BUDGET + 50_000


def test_db_snapshot_still_redacts_passwords(client):
    users = next(t for t in client.get("/api/demo/db").get_json()["tables"] if t["name"] == "users")
    assert {row[1] for row in users["rows"]} == {demo_routes.REDACTED}


def test_outbox_is_bounded(client, execute):
    for i in range(10):
        execute("INSERT INTO outbox (job_id, to_email, subject, html, created_at) VALUES (1, 'a@b.co', 's', ?, 'now')",
                "h" * 200_000)
    body = client.get("/api/demo/outbox").get_json()
    msgs = body["messages"]
    assert len(msgs) == 10 and all(m["truncated"] for m in msgs)
    assert max(len(m["html"]) for m in msgs) <= demo_routes.OUTBOX_HTML_MAX + len(demo_routes.TRUNCATED_MARK)
    assert sum(len(m["html"]) for m in msgs) <= demo_routes.OUTBOX_RESPONSE_BUDGET + 100


def test_outbox_small_messages_are_whole(client, execute):
    execute("INSERT INTO outbox (job_id, to_email, subject, html, created_at) VALUES (1, 'a@b.co', 's', '<p>hi</p>', 'now')")
    m = client.get("/api/demo/outbox").get_json()["messages"][0]
    assert m["html"] == "<p>hi</p>" and m["truncated"] is False


# --- B2: field length caps ------------------------------------------------------------------

def product(**over):
    body = {"product_name": "Kiwi", "category": "Dairy", "unit": "kg", "quantity": 1, "price": 1,
            "manufacture_date": "2026-01-01", "expiry_date": "2027-01-01"}
    body.update(over)
    return body


@pytest.mark.parametrize("field,limit,label", [("product_name", 60, "Product name"), ("unit", 20, "Unit"),
                                               ("category", 40, "Category")])
def test_product_field_caps(client, admin_token, field, limit, label):
    ok = client.post("/api/manager/add_product", json=product(**{field: "Dairy" if field == "category" else "a" * limit}),
                     headers=bearer(admin_token))
    assert ok.status_code == 201, ok.get_json()
    bad = client.post("/api/manager/add_product", json=product(**{field: "a" * (limit + 1)}), headers=bearer(admin_token))
    assert f"{label} is too long (at most {limit} characters)" == assert_error(bad, 400)


def test_edit_product_has_the_same_caps(client, admin_token):
    r = client.post("/api/manager/edit_product", json=product(product_id=1, product_name="a" * 61),
                    headers=bearer(admin_token))
    assert_error(r, 400)


def test_category_name_cap(client, admin_token):
    assert client.post("/api/manager/categories/add_category", json={"category_name": "c" * 40},
                       headers=bearer(admin_token)).status_code == 201
    assert_error(client.post("/api/manager/categories/add_category", json={"category_name": "c" * 41},
                             headers=bearer(admin_token)), 400)


def signup_body(**over):
    body = {"name": "N", "email": "cap@example.com", "password": "secret123", "phoneNo": "1234567890", "address": "1 Rd"}
    body.update(over)
    return body


@pytest.mark.parametrize("over,message", [
    ({"name": "n" * 61}, "Name is too long"),
    ({"address": "a" * 201}, "Address is too long"),
    ({"password": "p" * 129}, "Password is too long"),
    ({"email": "a" * 250 + "@example.com"}, "Email is too long"),
])
def test_signup_caps(client, over, message):
    assert message in assert_error(client.post("/api/signup", json=signup_body(**over)), 400)


def test_signup_at_the_caps_works(client):
    r = client.post("/api/signup", json=signup_body(name="n" * 60, address="a" * 200, password="p" * 128))
    assert r.status_code == 201


def test_login_password_cap(client):
    assert "too long" in assert_error(client.post("/api/login", json={"email": "demo@grocery.app", "password": "p" * 129}), 400)
    assert "too long" in assert_error(client.post("/api/admin/login", json={"email": "admin", "password": "p" * 129}), 400)


# --- B2: sandbox size cap ---------------------------------------------------------------------

def test_max_page_count_is_set_on_every_connection(app, client, admin_token):
    from applications.sandbox import open_connection
    with app.test_request_context("/", headers={"X-Sandbox-Id": client.sandbox_id}):
        from flask import g
        g.sandbox_id = client.sandbox_id
        conn = open_connection(app)
        try:
            assert conn.execute("PRAGMA max_page_count").fetchone()[0] == app.config["SANDBOX_MAX_PAGES"] == 2000
        finally:
            conn.close()


def test_a_full_sandbox_answers_507_sandbox_full(app, client, admin_token, monkeypatch):
    monkeypatch.setitem(app.config, "SANDBOX_MAX_PAGES", 30)
    last = None
    for i in range(2000):
        last = client.post("/api/manager/add_product", json=product(product_name=f"{i:04d}" + "A" * 56),
                           headers=bearer(admin_token))
        if last.status_code != 201:
            break
    assert last.status_code == 507, last.status_code
    assert last.get_json() == {"error": "This demo sandbox is full. Reset the demo to start fresh.",
                               "code": "sandbox_full"}
    # reads still work and the sandbox is usable (not corrupted)
    assert client.get("/api/demo/db").status_code == 200
    assert client.post("/api/demo/sql", json={"sql": "select count(*) from products"}).status_code == 200


def test_the_default_sandbox_cap_is_about_8mb(app):
    assert app.config["SANDBOX_MAX_PAGES"] * 4096 == 8_192_000


def test_the_default_rate_limit_is_configured_for_mutations_only(app):
    assert app.config["RATELIMIT_DEFAULT"] == "300 per minute"


def test_default_limit_counts_mutating_requests_only(tmp_path):
    import subprocess, sys, textwrap
    script = textwrap.dedent("""
        import app
        c = app.app.test_client()
        h = {"X-Sandbox-Id": c.post("/api/demo/session", json={"role": "user"}).get_json()["sandbox_id"]}
        assert all(c.get("/api/getCategories", headers=h).status_code == 200 for _ in range(8))
        codes = [c.post("/api/search", json={"search_query": "a"}, headers=h).status_code for _ in range(4)]
        assert codes[:2] == [200, 200] and codes[-1] == 429, codes
        assert c.get("/api/getCategories", headers=h).status_code == 200
        print("ok")
    """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX", "CORS", "JOBS"))}
    env.update(RATELIMIT_ENABLED="true", RATELIMIT_DEFAULT="3 per minute", SANDBOX_DIR=str(tmp_path / "sbx"),
               LOG_LEVEL="ERROR", APP_ENV="development", JOBS_EXECUTION="inline")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
