from flask import g, request
from flask_jwt_extended import get_jwt, get_jwt_identity, verify_jwt_in_request

from applications.store_manager import manager_check


def sandbox_scope():
    """Cache-key / token scope of the current request: the sandbox id in demo mode."""
    return g.get("sandbox_id") or "-"


def token_claims(role):
    """Extra JWT claims for a freshly issued token: the role, plus the sandbox
    it belongs to (demo mode) or the id of the single database (otherwise)."""
    from flask import current_app

    from applications.database import get_db_instance_id

    if current_app.config["DEMO_MODE"]:
        return {"role": role, "sbx": g.sandbox_id}
    return {"role": role, "dbid": get_db_instance_id()}


def public_cache_key():
    """Cache key for public (anonymous) endpoints; never shared between sandboxes."""
    return f"view:{sandbox_scope()}:{request.full_path}"


def is_admin():
    return get_jwt().get("role") == "admin"


def is_manager_or_admin():
    """Authorise from the server's own records, never from client-supplied flags."""
    return is_admin() or manager_check(get_jwt_identity())


def per_user_cache_key():
    """Cache key for flask-caching that keeps each user's responses separate."""
    verify_jwt_in_request()
    return f"view:{sandbox_scope()}:{request.full_path}:{get_jwt().get('role', 'user')}:{get_jwt_identity()}"
