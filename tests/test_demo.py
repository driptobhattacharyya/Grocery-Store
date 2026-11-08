"""POST /api/demo/session, /switch, GET /db and the read-only SQL console."""
import os
import sqlite3
import subprocess
import sys
import textwrap

import pytest

from helpers import APPLE, assert_error, bearer

REDACTED = "•" * 6
TABLE_ORDER = ["users", "user_profiles", "store_manager", "managers", "categories", "products", "inventory",
               "cart", "orders", "order_items", "coupons", "used_coupons", "jobs", "outbox"]


@pytest.fixture
def anon(app):
    """A client that sends no sandbox header (for POST /api/demo/session)."""
    client = app.test_client()
    client.sandbox_id = None
    return client


def _session(anon, role):
    response = anon.post("/api/demo/session", json={"role": role})
    assert response.status_code == 201, response.get_data(as_text=True)
    return response.get_json()


def _sql(client, sql, **kwargs):
    return client.post("/api/demo/sql", json={"sql": sql}, **kwargs)


# --- POST /api/demo/session --------------------------------------------------------------

@pytest.mark.parametrize("role,name,is_manager", [
    ("user", "Demo Shopper", False), ("manager", "Demo Manager", True), ("admin", "Administrator", True)])
def test_session_creates_a_sandbox_and_logs_in(app, anon, store, role, name, is_manager):
    body = _session(anon, role)
    assert body["role"] == role and body["name"] == name and body["is_manager"] is is_manager
    assert body["expires_in"] == 3600 and store.touch(body["sandbox_id"])
    assert body["accounts"] == [
        {"role": "user", "email": "demo@grocery.app", "password": "demo1234"},
        {"role": "manager", "email": "manager@grocery.app", "password": "manager1234"},
        {"role": "admin", "username": "admin", "password": "admin1234"},
    ]
    store.delete(body["sandbox_id"])


def test_session_tokens_work_for_their_role(anon, store):
    results = {}
    for role in ("user", "manager", "admin"):
        body = _session(anon, role)
        anon.sandbox_id = body["sandbox_id"]
        headers = bearer(body["access_token"])
        results[role] = (
            anon.get("/api/shopping_cart", headers=headers).status_code,
            anon.get("/api/manager/dashboard", headers=headers).status_code,
            anon.get("/api/admin/manager_requests", headers=headers).status_code,
        )
        store.delete(body["sandbox_id"])
        anon.sandbox_id = None
    # (cart, manager dashboard, admin requests)
    assert results == {"user": (200, 403, 403), "manager": (200, 200, 403), "admin": (200, 200, 200)}


def test_each_session_is_a_separate_sandbox(anon, store):
    a, b = _session(anon, "user"), _session(anon, "user")
    assert a["sandbox_id"] != b["sandbox_id"]
    assert a["access_token"] != b["access_token"]
    for body in (a, b):
        store.delete(body["sandbox_id"])


@pytest.mark.parametrize("payload", [{}, {"role": "root"}, {"role": ""}, {"role": None}, {"role": ["user"]}, {"role": 1}])
def test_session_rejects_bad_roles(anon, payload):
    assert_error(anon.post("/api/demo/session", json=payload), 400)


def test_session_rejects_non_json_bodies(anon):
    assert_error(anon.post("/api/demo/session", data="nope", content_type="text/plain"), 400)
    assert_error(anon.post("/api/demo/session", json=["user"]), 400)


def test_session_ignores_a_bogus_sandbox_header(anon, store):
    response = anon.post("/api/demo/session", json={"role": "user"}, headers={"X-Sandbox-Id": "../../x"})
    assert response.status_code == 201
    store.delete(response.get_json()["sandbox_id"])


def test_session_creation_evicts_the_oldest_when_full(app, anon, store, monkeypatch):
    monkeypatch.setattr(store, "max", store.count() + 2)
    first = _session(anon, "user")["sandbox_id"]
    second = _session(anon, "user")["sandbox_id"]
    third = _session(anon, "user")["sandbox_id"]
    assert store.touch(first) is None and store.touch(second) and store.touch(third)
    for sid in (second, third):
        store.delete(sid)


# --- POST /api/demo/switch ---------------------------------------------------------------

def test_switch_between_roles(client, sandbox_id):
    seen = {}
    for role in ("manager", "admin", "user"):
        response = client.post("/api/demo/switch", json={"role": role})
        assert response.status_code == 200
        body = response.get_json()
        assert body["sandbox_id"] == sandbox_id and body["role"] == role
        assert "accounts" not in body and body["expires_in"] == 3600
        seen[role] = (body["name"], body["is_manager"])
        assert client.get("/api/shopping_cart" if role != "admin" else "/api/admin/manager_requests",
                          headers=bearer(body["access_token"])).status_code == 200
    assert seen == {"manager": ("Demo Manager", True), "admin": ("Administrator", True),
                    "user": ("Demo Shopper", False)}


def test_switch_validates_input(client):
    assert_error(client.post("/api/demo/switch", json={}), 400)
    assert_error(client.post("/api/demo/switch", json={"role": "god"}), 400)
    assert_error(client.post("/api/demo/switch", json={"email": ["x"]}), 400)
    assert_error(client.post("/api/demo/switch", json={"email": "ghost@example.com"}), 404)


def test_switch_needs_a_live_sandbox(client, store, sandbox_id):
    client.sandbox_id = None
    assert client.post("/api/demo/switch", json={"role": "user"}).status_code == 410
    client.sandbox_id = sandbox_id
    store.delete(sandbox_id)
    response = client.post("/api/demo/switch", json={"role": "user"})
    assert response.status_code == 410 and response.get_json()["code"] == "sandbox_expired"


def test_signup_apply_approve_and_switch_to_my_account(client, signup, login):
    signup("visitor@example.com", "secret123", name="Visitor")
    token = login("visitor@example.com", "secret123")
    assert client.get("/api/manager/dashboard", headers=bearer(token)).status_code == 403
    assert client.post("/api/apply_manager", headers=bearer(token)).status_code == 200

    admin = client.post("/api/demo/switch", json={"role": "admin"}).get_json()
    pending = client.get("/api/admin/manager_requests", headers=bearer(admin["access_token"])).get_json()
    assert [r["email"] for r in pending["manager_requests"]] == ["visitor@example.com"]
    assert client.post("/api/admin/accept_manager", json={"email": "visitor@example.com"},
                       headers=bearer(admin["access_token"])).status_code == 200

    # Not an account listed in `accounts`: switch to the signed-up user by e-mail (case-insensitive).
    mine = client.post("/api/demo/switch", json={"email": " Visitor@Example.com "})
    assert mine.status_code == 200
    body = mine.get_json()
    assert body["role"] == "manager" and body["is_manager"] is True and body["name"] == "Visitor"
    assert client.get("/api/manager/dashboard", headers=bearer(body["access_token"])).status_code == 200
    assert client.get("/api/admin/manager_requests", headers=bearer(body["access_token"])).status_code == 403


def test_switch_by_email_to_a_plain_user_and_to_admin(client, signup):
    signup("plain@example.com")
    body = client.post("/api/demo/switch", json={"email": "plain@example.com"}).get_json()
    assert (body["role"], body["is_manager"]) == ("user", False)
    assert client.post("/api/demo/switch", json={"email": "admin"}).get_json()["role"] == "admin"


# --- GET /api/demo/db ----------------------------------------------------------------------

def test_db_snapshot_shape_and_order(client):
    response = client.get("/api/demo/db")
    assert response.status_code == 200
    tables = response.get_json()["tables"]
    assert [t["name"] for t in tables] == TABLE_ORDER
    assert "meta" not in [t["name"] for t in tables]
    by_name = {t["name"]: t for t in tables}
    products = by_name["products"]
    assert products["columns"][0] == "productID" and products["row_count"] == 14
    assert len(products["rows"]) == 14 and products["truncated"] is False
    assert products["rows"][0][:2] == [1, "Apple"]
    assert by_name["jobs"]["columns"] == ["id", "kind", "status", "to_email", "created_at", "updated_at", "detail"]
    assert by_name["outbox"]["columns"] == ["id", "job_id", "to_email", "subject", "html", "created_at"]
    assert by_name["jobs"]["rows"] == [] and by_name["jobs"]["row_count"] == 0


def test_db_snapshot_redacts_password_columns(client):
    tables = {t["name"]: t for t in client.get("/api/demo/db").get_json()["tables"]}
    for name in ("users", "managers"):
        col = tables[name]["columns"].index("password")
        assert tables[name]["rows"] and all(row[col] == REDACTED for row in tables[name]["rows"])
    assert "scrypt" not in str(tables) and "pbkdf2" not in str(tables)


def test_db_snapshot_reflects_changes_and_caps_rows(client, signup, execute):
    signup("snap@example.com")
    for i in range(205):
        execute("INSERT INTO coupons (coupon_code, discount_percentage) VALUES (?, 1)", f"BULK{i}")
    tables = {t["name"]: t for t in client.get("/api/demo/db").get_json()["tables"]}
    assert ["snap@example.com", REDACTED] in tables["users"]["rows"]
    coupons = tables["coupons"]
    assert coupons["truncated"] is True and len(coupons["rows"]) == 200 and coupons["row_count"] == 207
    assert coupons["rows"][0][0] == "FIRST50"  # stable order: insertion order


def test_db_snapshot_is_private_to_the_sandbox(client, sandbox_client, new_sandbox, signup):
    signup("mine@example.com")
    other = sandbox_client(new_sandbox())
    assert "mine@example.com" not in str(other.get("/api/demo/db").get_json())


# --- POST /api/demo/sql ----------------------------------------------------------------------

def test_sql_select(client):
    response = _sql(client, "SELECT name, price FROM products ORDER BY productID LIMIT 2")
    assert response.status_code == 200
    body = response.get_json()
    assert body["columns"] == ["name", "price"] and body["rows"] == [["Apple", 180.0], ["Banana", 48.0]]
    assert body["truncated"] is False and isinstance(body["elapsed_ms"], int)


@pytest.mark.parametrize("sql", ["SELECT 1;", "SELECT 1;  ", "SELECT 1; -- trailing comment",
                                 "WITH t AS (SELECT 1 AS x) SELECT x FROM t", "SELECT ';'", "SELECT 'a;b' AS \"c;d\""])
def test_sql_accepts_single_statements(client, sql):
    assert _sql(client, sql).status_code == 200


def test_sql_sees_this_sandboxes_data(client, signup):
    signup("sql@example.com")
    rows = _sql(client, "SELECT email FROM users ORDER BY email").get_json()["rows"]
    assert ["sql@example.com"] in rows


def test_sql_caps_rows_at_200(client):
    body = _sql(client, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 500) SELECT x FROM c").get_json()
    assert len(body["rows"]) == 200 and body["truncated"] is True
    body = _sql(client, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c WHERE x < 200) SELECT x FROM c").get_json()
    assert len(body["rows"]) == 200 and body["truncated"] is False


def test_sql_blobs_and_nulls_are_json(client):
    body = _sql(client, "SELECT randomblob(4), NULL, 1.5, 'x'").get_json()
    assert body["rows"] == [["<blob 4 bytes>", None, 1.5, "x"]]


@pytest.mark.parametrize("sql", ["", "   ", None, 5, ["SELECT 1"]])
def test_sql_requires_text(client, sql):
    response = client.post("/api/demo/sql", json={"sql": sql})
    assert response.status_code == 400 and response.get_json()["code"] == "sql_error"


def test_sql_error_shape(client):
    response = _sql(client, "SELEC nonsense")
    assert response.status_code == 400
    body = response.get_json()
    assert body["code"] == "sql_error" and "syntax error" in body["error"]
    assert _sql(client, "SELECT * FROM no_such_table").status_code == 400
    assert_error(client.post("/api/demo/sql", data="x", content_type="text/plain"), 400)


def test_sql_too_long(client):
    assert _sql(client, "SELECT '" + "a" * 6000 + "'").status_code == 400


def _assert_sql_refused(client, sql):
    response = _sql(client, sql)
    assert response.status_code == 400, (sql, response.get_data(as_text=True))
    assert response.get_json()["code"] == "sql_error"


@pytest.mark.parametrize("sql", [
    "INSERT INTO coupons VALUES ('HACK', 99)",
    "UPDATE products SET price = 0",
    "DELETE FROM users",
    "DROP TABLE users",
    "CREATE TABLE evil (x)",
    "CREATE TEMP TABLE evil (x)",
    "CREATE TRIGGER t AFTER INSERT ON cart BEGIN DELETE FROM users; END",
    "CREATE VIEW v AS SELECT 1",
    "ALTER TABLE users ADD COLUMN x",
    "REPLACE INTO coupons VALUES ('HACK', 99)",
    "WITH x AS (SELECT 1) DELETE FROM users",
    "PRAGMA table_info(users)",
    "PRAGMA writable_schema = 1",
    "PRAGMA journal_mode = DELETE",
    "PRAGMA query_only = 0",
    "SELECT * FROM pragma_table_info('users')",
    "ATTACH DATABASE ':memory:' AS m",
    "ATTACH DATABASE '/tmp/evil.db' AS evil",
    "DETACH DATABASE main",
    "VACUUM",
    "VACUUM INTO '/tmp/copy.db'",
    "REINDEX",
    "ANALYZE",
    "BEGIN",
    "COMMIT",
    "SAVEPOINT s",
    "SELECT load_extension('/lib/x86_64-linux-gnu/libc.so.6')",
    "SELECT load_extension('x', 'y')",
    "SELECT LOAD_EXTENSION('x')",
])
def test_sql_refuses_everything_but_selects(client, query, sql):
    before = query("SELECT COUNT(*) FROM users"), query("SELECT SUM(price) FROM products"), query("SELECT COUNT(*) FROM coupons")
    _assert_sql_refused(client, sql)
    assert (query("SELECT COUNT(*) FROM users"), query("SELECT SUM(price) FROM products"),
            query("SELECT COUNT(*) FROM coupons")) == before
    assert query("SELECT name FROM sqlite_master WHERE name IN ('evil', 'v', 't')") == []


@pytest.mark.parametrize("sql", [
    "SELECT 1; SELECT 2",
    "SELECT 1; DROP TABLE users",
    "SELECT 1;DELETE FROM users",
    "SELECT 1; /* x */ DELETE FROM users",
    "SELECT 1;\nSELECT 2;",
    "SELECT 1;;",
    "SELECT ';'; DELETE FROM users",
])
def test_sql_refuses_multiple_statements(client, query, sql):
    _assert_sql_refused(client, sql)
    assert query("SELECT COUNT(*) FROM users") == [(2,)]


def test_sql_cannot_read_password_columns(client):
    for sql in ("SELECT password FROM users", "SELECT * FROM users", "SELECT u.* FROM users u",
                "SELECT password FROM managers", "SELECT * FROM managers",
                "SELECT x.password FROM (SELECT * FROM users) x",
                "SELECT password FROM users UNION ALL SELECT password FROM managers",
                "SELECT length(password) FROM users", "SELECT substr(password, 1, 5) FROM users",
                "SELECT group_concat(password) FROM users", "SELECT hex(password) FROM users"):
        response = _sql(client, sql)
        assert response.status_code == 200, sql
        values = [cell for row in response.get_json()["rows"] for cell in row]
        assert "scrypt" not in str(values) and "pbkdf2" not in str(values), sql
        if "password" in sql.lower() and "*" not in sql:
            assert all(v in (None, "") for v in values), (sql, values)  # hex(NULL) is ''
    # No oracle either: predicates on a hidden column see NULL.
    body = _sql(client, "SELECT email FROM users WHERE password LIKE 'scrypt%' OR password IS NOT NULL").get_json()
    assert body["rows"] == []


def test_sql_runaway_query_is_stopped(client):
    import time

    started = time.monotonic()
    response = _sql(client, "WITH RECURSIVE c(x) AS (SELECT 1 UNION ALL SELECT x+1 FROM c) SELECT count(*) FROM c")
    assert time.monotonic() - started < 5
    assert response.status_code == 400 and response.get_json()["code"] == "sql_error"
    assert "too long" in response.get_json()["error"]
    # An expensive cross join is stopped as well.
    response = _sql(client, "SELECT count(*) FROM products a, products b, products c, products d, products e, products f, products g, products h")
    assert response.status_code == 400
    # The server is still healthy afterwards.
    assert _sql(client, "SELECT 1").status_code == 200


def test_sql_huge_values_are_refused(client):
    assert _sql(client, "SELECT zeroblob(2000000000)").status_code == 400
    assert _sql(client, "SELECT hex(zeroblob(900000000))").status_code == 400


def test_sql_cannot_reach_another_sandbox(client, sandbox_client, new_sandbox, store, signup):
    other_id = new_sandbox()
    other = sandbox_client(other_id)
    other.post("/api/signup", json={"name": "Secret", "email": "secret@elsewhere.com", "password": "secret123",
                                    "phoneNo": "1234567890"})
    # The only sandbox file this console opens is the one named by the header.
    assert "secret@elsewhere.com" not in str(_sql(client, "SELECT * FROM users").get_json())
    assert "secret@elsewhere.com" in str(_sql(other, "SELECT * FROM users").get_json())
    other_path = store.path_for(other_id)
    for sql in (f"ATTACH DATABASE '{other_path}' AS o",
                f"ATTACH '{other_path}' AS o; SELECT * FROM o.users",
                f"VACUUM INTO '{other_path}.copy'",
                f"SELECT * FROM o.users", "SELECT * FROM temp.sqlite_master"):
        response = _sql(client, sql)
        if response.status_code == 200:  # e.g. temp.sqlite_master: empty, nothing of the other sandbox
            assert "secret@elsewhere.com" not in str(response.get_json())
        else:
            assert response.status_code == 400
    assert not os.path.exists(other_path + ".copy")


def test_sql_sandbox_id_cannot_be_used_for_path_tricks(client, store):
    for value in (f"../{os.path.basename(store.template_path)}", os.path.join(store.directory, "template"),
                  "..%2ftemplate"):
        assert _sql(client, "SELECT 1", headers={"X-Sandbox-Id": value}).status_code == 410


def test_sql_connection_is_physically_read_only(app, sandbox_path):
    """Even without the authorizer the connection could not write."""
    from applications import demo_routes

    conn = demo_routes._open_readonly(sandbox_path)
    conn.set_authorizer(None)
    with pytest.raises(sqlite3.OperationalError, match="readonly"):
        conn.execute("DELETE FROM users")
    conn.close()


def test_sql_is_unavailable_without_a_sandbox(client):
    client.sandbox_id = None
    assert _sql(client, "SELECT 1").status_code == 410


# --- rate limits (the limiter is configured at import time: fresh interpreter) ------------------

def test_demo_endpoints_are_rate_limited(tmp_path):
    script = textwrap.dedent("""
        import app
        c = app.app.test_client()
        sessions = [c.post("/api/demo/session", json={"role": "user"}) for _ in range(4)]
        assert [r.status_code for r in sessions] == [201, 201, 201, 429], [r.status_code for r in sessions]
        assert sessions[3].headers.get("Retry-After") and sessions[3].get_json()["error"]
        h = {"X-Sandbox-Id": sessions[0].get_json()["sandbox_id"]}
        queries = [c.post("/api/demo/sql", json={"sql": "SELECT 1"}, headers=h).status_code for _ in range(5)]
        assert queries == [200, 200, 200, 200, 429], queries
        # Other demo endpoints are not affected by the SQL limit.
        assert c.get("/api/demo/db", headers=h).status_code == 200
        print("ok")
    """)
    root = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    env = {k: v for k, v in os.environ.items() if not k.startswith(("RATELIMIT", "DEMO_", "SANDBOX", "CORS"))}
    env.update(RATELIMIT_ENABLED="true", DEMO_SESSION_RATE_LIMIT="3 per hour", DEMO_SQL_RATE_LIMIT="4 per minute",
               SANDBOX_DIR=str(tmp_path / "sbx"), LOG_LEVEL="ERROR", APP_ENV="development")
    result = subprocess.run([sys.executable, "-c", script], cwd=root, env=env, capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
