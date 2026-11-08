"""Constants and assertion helpers shared by the test modules."""

DEMO_USER = ("demo@grocery.app", "demo1234")
DEMO_MANAGER = ("manager@grocery.app", "manager1234")
ADMIN = ("admin", "admin1234")  # DEMO_ADMIN_PASSWORD default, used by every sandbox

# Seeded product ids (see applications/database.py DEMO_CATALOG).
APPLE, BANANA, SPRITE, MILK = 1, 2, 7, 8
APPLE_PRICE, BANANA_PRICE, SPRITE_PRICE, MILK_PRICE = 180.0, 48.0, 40.0, 54.0
SPRITE_STOCK = 8


def bearer(token):
    return {"Authorization": f"Bearer {token}"}


# Set by the `sandbox_id` fixture: the sandbox of the running test.
CURRENT_SANDBOX = None


def mint_token(identity, role="user", sandbox_id=None, **kwargs):
    """An access token like the login endpoints issue, carrying the "sbx" claim
    of the test's sandbox (or ``sandbox_id``). Call inside ``app.app_context()``."""
    from flask_jwt_extended import create_access_token

    claims = {"role": role, "sbx": sandbox_id or CURRENT_SANDBOX}
    claims.update(kwargs.pop("additional_claims", {}))
    return create_access_token(identity=identity, additional_claims=claims, **kwargs)


def assert_error(response, status):
    """Every API error is JSON ``{"error": "<message>"}`` with the right status."""
    assert response.status_code == status, (response.status_code, response.get_data(as_text=True))
    assert response.is_json, response.get_data(as_text=True)
    body = response.get_json()
    assert isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"], body
    return body["error"]
