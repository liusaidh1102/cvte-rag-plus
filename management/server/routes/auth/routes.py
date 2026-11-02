"""Unified login and logout for both frontends.

``POST /api/v1/auth/login`` is the single credential endpoint. The optional
``channel`` field chooses the frontend policy (``web`` or ``management``), but
credential verification and token issuance stay in the shared auth service.
The legacy ``/v1/user/login`` endpoint delegates to the same service.
"""

import logging

from flask import jsonify, request

from api import settings
from api.auth.login_service import (
    MANAGEMENT_CHANNEL,
    WEB_CHANNEL,
    LoginError,
    LoginForbidden,
    LoginUnavailable,
    authenticate_login,
)
from api.auth.token_session import (
    create_token_session,
    delete_token_session,
    extract_bearer_token,
)
from services.auth import get_current_user_from_token

from .. import auth_bp


@auth_bp.route("/login", methods=["POST"])
def login():
    data = request.get_json(silent=True) or {}
    identifier = data.get("username") or data.get("email") or data.get("identifier")
    password = data.get("password")
    if not identifier or not password:
        return jsonify({"code": 1, "message": "用户名和密码不能为空"}), 400

    requested_channel = str(data.get("channel") or MANAGEMENT_CHANNEL).strip().lower()
    password_encrypted = data.get("password_encrypted")
    if isinstance(password_encrypted, str):
        password_encrypted = password_encrypted.strip().lower() in ("1", "true", "yes")
    elif password_encrypted is not None:
        password_encrypted = bool(password_encrypted)

    try:
        identity = authenticate_login(
            str(identifier),
            str(password),
            channel=requested_channel,
            password_encrypted=password_encrypted,
        )
    except LoginForbidden as exc:
        return jsonify({"code": 403, "message": str(exc)}), 403
    except LoginUnavailable as exc:
        return jsonify({"code": 503, "message": str(exc)}), 503
    except LoginError as exc:
        if requested_channel == WEB_CHANNEL:
            # Keep the legacy web contract: HTTP 200 + body-level auth code.
            return jsonify({"code": settings.RetCode.AUTHENTICATION_ERROR, "message": str(exc)})
        return jsonify({"code": 1, "message": str(exc)}), 400
    except Exception:
        # Never leak database or crypto details through the login endpoint.
        return jsonify({"code": 503, "message": "登录服务暂时不可用"}), 503

    try:
        token, expires_in = create_token_session(identity.session_claims())
    except Exception:
        logging.exception("Failed to create Redis login session")
        return jsonify({"code": 503, "message": "登录服务暂时不可用"}), 503

    data = identity.public_data()
    data.update(
        {
            "token": token,
            "access_token": token,
            "expiresIn": expires_in,
            "expires_in": expires_in,
            "role": identity.role,
            "username": identity.username,
            "nickname": identity.username,
            "tenantId": identity.tenant_id,
            "tenant_id": identity.tenant_id,
            "channel": requested_channel,
        }
    )
    response = jsonify({"code": 0, "message": "登录成功", "data": data})
    response.headers["Authorization"] = f"Bearer {token}"
    response.headers["Access-Control-Expose-Headers"] = "Authorization"
    return response


@auth_bp.route("/logout", methods=["POST"])
def logout():
    user = get_current_user_from_token()
    if user is None:
        return jsonify({"code": 401, "message": "未提供有效的认证令牌"}), 401
    token = extract_bearer_token(request.headers.get("Authorization"))
    delete_token_session(token)
    return jsonify({"code": 0, "message": "退出成功", "data": True})
