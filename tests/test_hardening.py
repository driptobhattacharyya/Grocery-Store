"""Regression tests for the deployment-hardening fixes: tokens tied to the
database they were issued for, atomic cart writes, CSV escaping, request size
limit, e-mail normalisation and admin-account cleanup."""
import csv
import io
import sqlite3
import threading

import pytest

from applications import login_manager
from applications.database import init_db
from applications.manager_controller import _csv_safe
from helpers import ADMIN, APPLE, DEMO_USER, SPRITE, SPRITE_STOCK, assert_error, bearer, mint_token


# --- tokens vs. the sandbox they were issued for -----------------------------------

def test_login_tokens_carry_the_sandbox_id(app, client, sandbox_id):
    from flask_jwt_extended import decode_token

    token = client.post("/api/login", json={"email": DEMO_USER[0], "password": DEMO_USER[1]}).get_json()["access_token"]
    admin = client.post("/api/admin/login", json={"email": ADMIN[0], "password": ADMIN[1]}).get_json()["access_token"]
    with app.app_context():
        assert decode_token(token)["sbx"] == sandbox_id
        assert decode_token(admin)["sbx"] == sandbox_id


def test_token_from_another_sandbox_is_rejected(app, client, sandbox_client, new_sandbox, shopper_token,
                                                manager_token, admin_token):
    other = sandbox_client(new_sandbox())  # the same demo accounts exist there too
    assert_error(other.get("/api/shopping_cart", headers=bearer(shopper_token)), 401)
    assert_error(other.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1},
                            headers=bearer(shopper_token)), 401)
    assert_error(other.post("/api/shopping_cart/checkout", headers=bearer(shopper_token)), 401)
    assert_error(other.get("/api/manager/dashboard", headers=bearer(manager_token)), 401)
    assert_error(other.get("/api/admin/manager_requests", headers=bearer(admin_token)), 401)
    # ... but they keep working in their own sandbox, and logging in at the other one works.
    assert client.get("/api/shopping_cart", headers=bearer(shopper_token)).status_code == 200
    token = other.post("/api/login", json={"email": DEMO_USER[0], "password": DEMO_USER[1]}).get_json()["access_token"]
    assert other.get("/api/shopping_cart", headers=bearer(token)).status_code == 200


def test_token_of_an_expired_sandbox_gets_410(app, client, store, sandbox_id, shopper_token):
    store.delete(sandbox_id)
    response = client.get("/api/shopping_cart", headers=bearer(shopper_token))
    assert response.status_code == 410 and response.get_json()["code"] == "sandbox_expired"


def test_old_token_cannot_read_a_same_named_account_elsewhere(app, client, sandbox_client, new_sandbox, signup, login):
    signup("someone@example.com", "secret123", name="Original Owner")
    old_token = login("someone@example.com", "secret123")
    other = sandbox_client(new_sandbox())
    other.post("/api/signup", json={"name": "New Owner", "email": "someone@example.com", "password": "different9",
                                    "phoneNo": "1234567890"})
    assert_error(other.get("/api/dashboard", headers=bearer(old_token)), 401)


def test_token_for_a_deleted_user_is_rejected(client, signup, login, execute):
    signup("gone@example.com", "secret123")
    token = login("gone@example.com", "secret123")
    assert client.get("/api/dashboard", headers=bearer(token)).status_code == 200
    execute("DELETE FROM user_profiles WHERE email = 'gone@example.com'")
    execute("DELETE FROM users WHERE email = 'gone@example.com'")
    assert_error(client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1}, headers=bearer(token)), 401)
    assert_error(client.post("/api/shopping_cart/checkout", headers=bearer(token)), 401)


def test_admin_token_is_checked_against_the_managers_table(app, client):
    with app.app_context():
        ghost = mint_token("not-the-admin", role="admin")
        # A shopper identity with an admin role claim isn't an admin account either.
        shopper_as_admin = mint_token(DEMO_USER[0], role="admin")
    assert_error(client.get("/api/admin/manager_requests", headers=bearer(ghost)), 401)
    assert_error(client.get("/api/admin/manager_requests", headers=bearer(shopper_as_admin)), 401)


def test_token_without_sandbox_claim_is_rejected(app, client):
    from flask_jwt_extended import create_access_token

    with app.app_context():
        legacy = create_access_token(identity=DEMO_USER[0], additional_claims={"role": "user"})
        legacy_dbid = create_access_token(identity=DEMO_USER[0], additional_claims={"role": "user", "dbid": "x"})
    assert_error(client.get("/api/shopping_cart", headers=bearer(legacy)), 401)
    assert_error(client.get("/api/shopping_cart", headers=bearer(legacy_dbid)), 401)


def test_database_id_survives_restarts(app, sandbox_path, query):
    (before,), = query("SELECT value FROM meta WHERE key = 'db_instance_id'")
    init_db(app, path=sandbox_path)  # a restart with the database still on disk
    assert query("SELECT value FROM meta WHERE key = 'db_instance_id'") == [(before,)]


def test_every_sandbox_has_its_own_database_id(store, sandbox_id, new_sandbox):
    ids = set()
    for sid in (sandbox_id, new_sandbox()):
        conn = sqlite3.connect(store.path_for(sid))
        ids.add(conn.execute("SELECT value FROM meta WHERE key = 'db_instance_id'").fetchone()[0])
        conn.close()
    assert len(ids) == 2


# --- admin account cleanup ----------------------------------------------------------

def test_startup_removes_admin_rows_for_other_usernames(app, tmp_path, monkeypatch):
    path = str(tmp_path / "admins.db")
    init_db(app, path=path, admin_password="whatever-pass")
    conn = sqlite3.connect(path)
    conn.execute("INSERT INTO managers (email, password) VALUES ('old-admin', 'x')")
    conn.commit()
    monkeypatch.setitem(app.config, "ADMIN_USERNAME", "boss")
    init_db(app, path=path, admin_password="whatever-pass")
    assert conn.execute("SELECT email FROM managers").fetchall() == [("boss",)]
    conn.close()


def test_admin_uses_the_demo_admin_password(client, app):
    assert app.config["DEMO_ADMIN_PASSWORD"] == ADMIN[1]
    assert client.post("/api/admin/login", json={"email": ADMIN[0], "password": ADMIN[1]}).status_code == 200
    assert_error(client.post("/api/admin/login", json={"email": ADMIN[0], "password": "password"}), 401)


# --- cart integrity -----------------------------------------------------------------

def test_concurrent_add_to_cart_does_not_lose_updates(sandbox_client, shopper_token, query):
    n = 8
    barrier = threading.Barrier(n)
    statuses = []

    def add():
        client = sandbox_client()
        barrier.wait()
        response = client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 1},
                               headers=bearer(shopper_token))
        statuses.append(response.status_code)

    threads = [threading.Thread(target=add) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert statuses == [200] * n
    assert query("SELECT COUNT(*), SUM(quantity) FROM cart WHERE email = ? AND product_id = ?",
                 DEMO_USER[0], APPLE) == [(1, n)]


def test_concurrent_add_to_cart_respects_stock(sandbox_client, shopper_token, query):
    # SPRITE_STOCK + 4 parallel adds of 1: exactly SPRITE_STOCK may succeed.
    n = SPRITE_STOCK + 4
    barrier = threading.Barrier(n)
    statuses = []

    def add():
        client = sandbox_client()
        barrier.wait()
        statuses.append(client.post("/api/add_to_cart", json={"product_id": SPRITE, "quantity": 1},
                                    headers=bearer(shopper_token)).status_code)

    threads = [threading.Thread(target=add) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert sorted(statuses) == [200] * SPRITE_STOCK + [409] * 4
    assert query("SELECT SUM(quantity) FROM cart WHERE product_id = ?", SPRITE) == [(SPRITE_STOCK,)]


def test_cart_has_a_unique_index_per_user_and_product(app, execute):
    execute("INSERT INTO cart (email, product_id, quantity) VALUES (?, ?, 1)", DEMO_USER[0], APPLE)
    with pytest.raises(sqlite3.IntegrityError):
        execute("INSERT INTO cart (email, product_id, quantity) VALUES (?, ?, 1)", DEMO_USER[0], APPLE)


def test_startup_merges_duplicate_cart_rows_from_older_databases(app, sandbox_path, execute, query):
    execute("DROP INDEX cart_email_product")
    for quantity in (2, 3, 4):
        execute("INSERT INTO cart (email, product_id, quantity) VALUES (?, ?, ?)", DEMO_USER[0], APPLE, quantity)
    execute("INSERT INTO cart (email, product_id, quantity) VALUES (?, ?, 1)", DEMO_USER[0], SPRITE)

    init_db(app, path=sandbox_path)

    assert query("SELECT product_id, quantity FROM cart WHERE email = ? ORDER BY product_id", DEMO_USER[0]) == [
        (APPLE, 9), (SPRITE, 1)]
    assert query("SELECT name FROM sqlite_master WHERE type = 'index' AND name = 'cart_email_product'") == [
        ("cart_email_product",)]


# --- sales report CSV -----------------------------------------------------------------

@pytest.mark.parametrize("value,expected", [
    ("=HYPERLINK(\"http://x\")", "'=HYPERLINK(\"http://x\")"),
    ("+1+1", "'+1+1"),
    ("-2+3", "'-2+3"),
    ("@SUM(A1)", "'@SUM(A1)"),
    ("\tTab", "'\tTab"),
    ("\rCR", "'\rCR"),
    ("Apple", "Apple"),
    (-5, -5),
    (12.5, 12.5),
])
def test_csv_safe(value, expected):
    assert _csv_safe(value) == expected


def test_report_escapes_formula_product_names(client, manager_token, execute):
    execute("UPDATE products SET name = '=cmd|calc' WHERE productID = ?", APPLE)
    execute("INSERT INTO orders (orderID, email, order_date, total_price) VALUES (900, ?, '2026-01-01', 360)",
            DEMO_USER[0])
    execute("INSERT INTO order_items (orderID, product_name, quantity) VALUES (900, '=cmd|calc', 2)")

    response = client.get("/api/manager/prepare_report", headers=bearer(manager_token))
    assert response.status_code == 200
    rows = list(csv.reader(io.StringIO(response.get_data(as_text=True))))
    assert rows[1:] == [["'=cmd|calc", "2", "360.0"]]


# --- request size limit -----------------------------------------------------------------

def test_oversized_body_is_json_413(app, client, shopper_token):
    big = "x" * (app.config["MAX_CONTENT_LENGTH"] + 1)
    assert_error(client.post("/api/login", data=big, content_type="application/json"), 413)
    # Also on a JWT-protected route whose errors go through api_errors.
    assert_error(client.post("/api/add_to_cart", data=big, content_type="application/json",
                             headers=bearer(shopper_token)), 413)


# --- e-mail normalisation and login timing -------------------------------------------------

def test_emails_are_trimmed_and_case_insensitive(client, query, login):
    response = client.post("/api/signup", json={
        "name": "Mixed", "email": "  Mixed.Case@Example.COM ", "password": "secret123", "phoneNo": "1234567890"})
    assert response.status_code == 201
    assert query("SELECT email FROM users WHERE email LIKE 'mixed%'") == [("mixed.case@example.com",)]
    login("MIXED.case@example.com", "secret123")
    login(" Demo@Grocery.App ", DEMO_USER[1])
    # The same address in another case is the same account.
    assert_error(client.post("/api/signup", json={
        "name": "Dup", "email": "mixed.CASE@example.com", "password": "secret123", "phoneNo": "1234567890"}), 409)
    response = client.post("/api/admin/login", json={"email": " ADMIN ", "password": ADMIN[1]})
    assert response.status_code == 200


def test_unknown_email_still_checks_a_password_hash(client, monkeypatch):
    calls = []
    real = login_manager.check_password_hash
    monkeypatch.setattr(login_manager, "check_password_hash", lambda *a: calls.append(a) or real(*a))
    assert_error(client.post("/api/login", json={"email": "nobody@example.com", "password": "whatever"}), 401)
    assert_error(client.post("/api/admin/login", json={"email": "nobody", "password": "whatever"}), 401)
    assert len(calls) == 2
