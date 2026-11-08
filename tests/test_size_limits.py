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
