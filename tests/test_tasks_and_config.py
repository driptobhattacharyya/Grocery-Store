"""The production config check (e-mail jobs are tested in test_email.py)."""
import pytest

from applications.config import DEV_SECRET_KEY, ConfigError, validate_config


# --- validate_config (in-process; the subprocess tests are in test_infra.py) ---------

STRONG = {"SECRET_KEY": "s" * 40, "JWT_SECRET_KEY": "j" * 40, "ADMIN_PASSWORD": "long-enough-pw",
          "DEMO_MODE": "false"}  # single-DB mode: the operator's ADMIN_PASSWORD is checked


def _config(monkeypatch, **env):
    for key in ("SECRET_KEY", "JWT_SECRET_KEY", "ADMIN_PASSWORD", "DEMO_MODE"):
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    return {"IS_PRODUCTION": True, "DEMO_MODE": env.get("DEMO_MODE") != "false", "SECRET_KEY": env.get("SECRET_KEY", DEV_SECRET_KEY),
            "JWT_SECRET_KEY": env.get("JWT_SECRET_KEY", DEV_SECRET_KEY)}


def test_validate_config_ignores_development():
    validate_config({"IS_PRODUCTION": False, "SECRET_KEY": DEV_SECRET_KEY})


def test_validate_config_accepts_strong_secrets(monkeypatch):
    validate_config(_config(monkeypatch, **STRONG))


@pytest.mark.parametrize("env,problem", [
    ({}, "SECRET_KEY is not set"),
    ({**STRONG, "SECRET_KEY": DEV_SECRET_KEY}, "SECRET_KEY uses the development default"),
    ({**STRONG, "JWT_SECRET_KEY": "x" * 31}, "JWT_SECRET_KEY is too short"),
    ({**STRONG, "ADMIN_PASSWORD": "password"}, 'must not be "password"'),
    ({**STRONG, "ADMIN_PASSWORD": "1234567"}, "at least 8 characters"),
])
def test_validate_config_rejects_weak_secrets(monkeypatch, env, problem):
    with pytest.raises(ConfigError, match=problem):
        validate_config(_config(monkeypatch, **env))


def test_validate_config_demo_mode_does_not_need_admin_password(monkeypatch):
    # In demo mode each sandbox's admin uses DEMO_ADMIN_PASSWORD (public by design).
    strong = {"SECRET_KEY": "s" * 40, "JWT_SECRET_KEY": "j" * 40}
    validate_config(_config(monkeypatch, **strong))
    validate_config(_config(monkeypatch, **strong, ADMIN_PASSWORD="password"))  # ignored


def test_validate_config_demo_mode_still_needs_strong_keys(monkeypatch):
    with pytest.raises(ConfigError, match="SECRET_KEY is not set"):
        validate_config(_config(monkeypatch))
