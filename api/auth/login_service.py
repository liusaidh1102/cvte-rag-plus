"""Shared credential authentication for web and management login.

Both frontends use the same login endpoint and Redis-backed session format.
The channel only controls which roles may enter that frontend; credential
verification, claims construction and token persistence stay in one place.
"""

from __future__ import annotations

import base64
import os
import secrets
from dataclasses import dataclass
from typing import Any

from werkzeug.security import check_password_hash

from api.db import StatusEnum, UserTenantRole
from api.db.db_models import DB, User, UserTenant

WEB_CHANNEL = "web"
MANAGEMENT_CHANNEL = "management"
SUPPORTED_CHANNELS = {WEB_CHANNEL, MANAGEMENT_CHANNEL}


class LoginError(ValueError):
    """A credential failure that is safe to return to the client."""


class LoginForbidden(LoginError):
    """Credentials are valid, but this role may not use the requested channel."""


class LoginUnavailable(LoginError):
    """The authentication dependency (database, Redis, etc.) is unavailable."""


@dataclass
class AuthenticatedIdentity:
    user_id: str
    username: str
    email: str | None
    role: str
    tenant_id: str | None
    is_superuser: bool
    login_channel: str
    user: Any = None

    def session_claims(self) -> dict[str, Any]:
        return {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "tenant_id": self.tenant_id,
            "is_superuser": self.is_superuser,
            "login_channel": self.login_channel,
        }

    def public_data(self) -> dict[str, Any]:
        data = {}
        if self.user is not None:
            data.update(dict(self.user.to_dict()))
            data.pop("password", None)
            data.pop("access_token", None)
        data.update(
            {
                "id": self.user_id,
                "user_id": self.user_id,
                "username": self.username,
                "nickname": self.username,
                "email": self.email,
                "role": self.role,
                "tenant_id": self.tenant_id,
                "tenantId": self.tenant_id,
                "is_superuser": self.is_superuser,
            }
        )
        return data

    def management_info(self) -> dict[str, Any]:
        return {
            "id": self.user_id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "tenant_id": self.tenant_id,
            "is_superuser": self.is_superuser,
        }


def normalize_channel(channel: Any) -> str:
    value = str(channel or MANAGEMENT_CHANNEL).strip().lower()
    if value not in SUPPORTED_CHANNELS:
        raise LoginError("不支持的登录端")
    return value


def _resolve_password(password: str, encrypted: bool | None, channel: str) -> str:
    # Management users submit plain text; the web form submits RSA ciphertext.
    should_decrypt = encrypted is True or (encrypted is None and channel == WEB_CHANNEL)
    if not should_decrypt:
        return password
    try:
        from api.utils import decrypt

        return decrypt(password)
    except Exception as exc:
        if encrypted is True:
            raise LoginError("密码解密失败") from exc
        # Older web clients occasionally sent plain text. Keep that path usable
        # while new clients explicitly mark encrypted passwords.
        return password


def _authenticate_environment_admin(
    identifier: str, password: str, channel: str
) -> AuthenticatedIdentity | None:
    if channel != MANAGEMENT_CHANNEL:
        return None

    admin_username = os.getenv("MANAGEMENT_ADMIN_USERNAME", "admin").strip()
    if identifier != admin_username:
        return None

    # Imported lazily because the management server path is added during app
    # assembly, after the core API modules have already been imported.
    try:
        from admin_config import get_admin_password

        expected = get_admin_password()
    except Exception as exc:
        raise LoginUnavailable("登录服务暂时不可用") from exc
    if not secrets.compare_digest(str(password), str(expected)):
        raise LoginError("密码错误")
    return AuthenticatedIdentity(
        user_id="admin",
        username=admin_username,
        email=None,
        role="admin",
        tenant_id=None,
        is_superuser=True,
        login_channel=MANAGEMENT_CHANNEL,
    )


def _password_matches(password: str, stored_hash: str, channel: str) -> bool:
    # Both frontends ultimately hash the base64 form of the password. Older
    # records and some management scripts used the plain form, and web clients
    # send an RSA payload that decrypts to the base64 form. Try the compatible
    # encodings without ever trusting a client-supplied hash.
    candidates = [password]
    try:
        decoded = base64.b64decode(password, validate=True).decode("utf-8")
        if decoded != password:
            candidates.append(decoded)
    except (ValueError, UnicodeDecodeError):
        pass
    encoded = base64.b64encode(password.encode("utf-8")).decode("ascii")
    if encoded not in candidates:
        candidates.append(encoded)
    stored_hash = str(stored_hash or "")
    for candidate in candidates:
        try:
            if check_password_hash(stored_hash, candidate):
                return True
        except (TypeError, ValueError):
            continue
    return False


def authenticate_login(
    identifier: str,
    password: str,
    *,
    channel: str = MANAGEMENT_CHANNEL,
    password_encrypted: bool | None = None,
) -> AuthenticatedIdentity:
    """Authenticate one credential pair for either frontend."""
    channel = normalize_channel(channel)
    identifier = str(identifier or "").strip()
    password = str(password or "")
    if not identifier or not password:
        raise LoginError("用户名/邮箱和密码不能为空")

    password = _resolve_password(password, password_encrypted, channel)
    admin_identity = _authenticate_environment_admin(identifier, password, channel)
    if admin_identity is not None:
        return admin_identity

    try:
        with DB.connection_context():
            user = (
                User.select()
                .where(
                    (User.email == identifier) | (User.nickname == identifier),
                    User.status == StatusEnum.VALID.value,
                )
                .first()
            )
            if not user or not _password_matches(password, user.password, channel):
                raise LoginError("用户名、邮箱或密码错误")

            owner_record = (
                UserTenant.select(UserTenant.tenant_id)
                .where(
                    UserTenant.user_id == user.id,
                    UserTenant.role == UserTenantRole.OWNER.value,
                    UserTenant.status == StatusEnum.VALID.value,
                )
                .first()
            )
    except LoginError:
        raise
    except Exception as exc:
        raise LoginUnavailable("登录服务暂时不可用") from exc

    role = (
        "admin"
        if user.is_superuser
        else "team_owner"
        if owner_record
        else "user"
    )
    if channel == MANAGEMENT_CHANNEL and role not in ("admin", "team_owner"):
        raise LoginForbidden("无权限登录管理系统，只有超级管理员和团队负责人可以登录")

    return AuthenticatedIdentity(
        user_id=str(user.id),
        username=user.nickname,
        email=user.email,
        role=role,
        tenant_id=owner_record.tenant_id if owner_record else str(user.id),
        is_superuser=bool(user.is_superuser),
        login_channel=user.login_channel or "password",
        user=user,
    )
