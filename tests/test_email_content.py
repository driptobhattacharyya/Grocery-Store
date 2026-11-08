"""S1: no visitor-controlled text in e-mails."""
import pytest

from applications.email_templates import (
    NEUTRAL_ACCOUNT, NEUTRAL_PRODUCT, NEUTRAL_UNIT, SEEDED_NAMES, render, safe_account, safe_product_name, safe_unit,
)
from helpers import APPLE, bearer

PHISH = "URGENT: your account is locked. Verify at http://evil.example/login or pay bit.ly/xyz"


@pytest.fixture
def set_email(client):
    return lambda: client.post("/api/demo/email", json={"email": "v@example.com"})


def outbox_html(client):
    return client.get("/api/demo/outbox").get_json()["messages"][0]["html"]


def test_seeded_names_come_from_the_catalog():
    assert {"Apple", "Milk", "Top Ramen"} <= SEEDED_NAMES


def test_helpers():
    assert safe_product_name("Apple") == "Apple"
    assert safe_product_name(PHISH) == NEUTRAL_PRODUCT
    assert safe_product_name("Apple ") == NEUTRAL_PRODUCT          # exact match only
    assert safe_product_name("Apple", product_id=1) == "Apple"
    assert safe_product_name("Milk", product_id=1) == NEUTRAL_PRODUCT   # a seeded name, but not this product's
    assert safe_product_name("Apple", product_id=9999) == NEUTRAL_PRODUCT
    assert safe_unit("kg", 1) == "kg" and safe_unit("see evil.example", 1) == NEUTRAL_UNIT
    assert safe_account("demo@grocery.app") == "demo@grocery.app"
    assert safe_account("manager@grocery.app") == "manager@grocery.app"
    assert safe_account("admin") == "admin"
    assert safe_account("pay-now@evil.example") == NEUTRAL_ACCOUNT


def test_reminder_with_the_reviewers_phishing_name(client, set_email, execute):
    execute("UPDATE products SET name = ?, unit = ? WHERE productID = 1", PHISH, "visit evil.example")
    set_email()
    assert client.post("/api/demo/jobs", json={"kind": "daily_reminder"}).status_code == 202
    html = outbox_html(client)
    assert "URGENT" not in html and "evil" not in html and "bit.ly" not in html
    assert NEUTRAL_PRODUCT in html and "Banana" in html  # untouched products keep their names


def test_reminder_unchanged_for_seeded_data(client, set_email):
    set_email()
    client.post("/api/demo/jobs", json={"kind": "daily_reminder"})
    html = outbox_html(client)
    assert "Apple - 180.00 INR per kg" in html and NEUTRAL_PRODUCT not in html


def test_renamed_to_another_seeded_name_is_still_a_seeded_text(client, set_email, execute):
    execute("UPDATE products SET name = 'Banana' WHERE productID = 1")
    set_email()
    client.post("/api/demo/jobs", json={"kind": "daily_reminder"})
    assert NEUTRAL_PRODUCT in outbox_html(client)  # id 1 is not "Banana" by seed


def test_order_confirmation_with_phishing_name(client, set_email, execute, shopper_token):
    execute("UPDATE products SET name = ? WHERE productID = ?", PHISH, APPLE)
    set_email()
    client.post("/api/add_to_cart", json={"product_id": APPLE, "quantity": 2}, headers=bearer(shopper_token))
    order = client.post("/api/shopping_cart/checkout", headers=bearer(shopper_token)).get_json()
    html = outbox_html(client)
    assert "URGENT" not in html and "evil" not in html
    assert NEUTRAL_PRODUCT in html and f"#{order['order_id']}" in html and "360.00 INR" in html


def test_monthly_report_for_a_visitor_account_hides_the_address(client, set_email, signup, login):
    signup("pay-now@evil.example")
    token = login("pay-now@evil.example", "secret123")
    set_email()
    r = client.post("/api/demo/jobs", json={"kind": "monthly_report"}, headers=bearer(token))
    assert r.status_code == 202
    html = outbox_html(client)
    assert "evil.example" not in html and NEUTRAL_ACCOUNT in html


def test_monthly_report_for_the_demo_account_names_it(client, set_email, shopper_token):
    set_email()
    client.post("/api/demo/jobs", json={"kind": "monthly_report"}, headers=bearer(shopper_token))
    assert "demo@grocery.app" in outbox_html(client)


def test_manager_account_in_the_report(client, set_email, manager_token):
    set_email()
    client.post("/api/demo/jobs", json={"kind": "monthly_report"}, headers=bearer(manager_token))
    assert "manager@grocery.app" in outbox_html(client)


def test_render_itself_is_still_autoescaped():
    _, html = render("daily_reminder", products=[{"name": "<i>x</i>", "price": 1.0, "unit": "kg"}])
    assert "&lt;i&gt;" in html
