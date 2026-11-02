"""Management token validation shared by the request guard and route handlers."""

from flask import request

from api.auth.token_session import extract_bearer_token, get_token_session


def _nonempty_string(value):
    return isinstance(value, str) and bool(value.strip())


def get_current_user_from_token():
    """Return validated, request-local claims, or None. Never infer an admin role."""
    # Flask's g can outlive a request when an application context is held by
    # a caller. Cache on this request only, never on a shared app context.
    cache_key = "management.authenticated_user"
    if cache_key in request.environ:
        return request.environ[cache_key]

    request.environ[cache_key] = None
    token = extract_bearer_token(request.headers.get("Authorization"))
    payload = get_token_session(token)
    if not payload:
        return None

    if not _nonempty_string(payload.get("user_id")) or not _nonempty_string(payload.get("username")):
        return None
    role = payload.get("role")
    if role not in ("admin", "team_owner"):
        return None
    tenant_id = payload.get("tenant_id")
    if role == "team_owner" and not _nonempty_string(tenant_id):
        return None
    user = {
        "user_id": payload["user_id"],
        "username": payload["username"],
        "role": role,
        "tenant_id": tenant_id,
    }
    request.environ[cache_key] = user
    return user


def is_admin(user_info):
    """检查用户是否是超级管理员"""
    return bool(user_info) and user_info.get("role") == "admin"


def is_team_owner(user_info):
    """检查用户是否是团队负责人"""
    return bool(user_info) and user_info.get("role") == "team_owner"
