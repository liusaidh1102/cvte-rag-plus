"""Shared web / management credential service regressions."""

import base64
import os
import sys
import unittest
from contextlib import nullcontext
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from flask import Flask
from werkzeug.security import generate_password_hash

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parents[1]
for path in (SERVER_ROOT, REPO_ROOT):
    if str(path) not in sys.path:
        sys.path.insert(0, str(path))

from admin_config import ADMIN_PASSWORD_ENV  # noqa: E402
from api.auth import login_service  # noqa: E402


def query_result(row):
    query = MagicMock()
    query.where.return_value.first.return_value = row
    return query


class LoginServiceTests(unittest.TestCase):
    def setUp(self):
        self.password = "correct horse battery staple"
        self.stored_hash = generate_password_hash(
            base64.b64encode(self.password.encode()).decode()
        )
        self.user = SimpleNamespace(
            id="user-1",
            nickname="alice",
            email="alice@example.com",
            password=self.stored_hash,
            is_superuser=False,
            login_channel="password",
            status="1",
        )
        self.user_select = query_result(self.user)
        self.owner_select = query_result(SimpleNamespace(tenant_id="tenant-1"))

    def auth(self, *, identifier, password, channel, encrypted, owner=True):
        tenant_query = query_result(SimpleNamespace(tenant_id="tenant-1") if owner else None)
        with patch.object(login_service.DB, "connection_context", return_value=nullcontext()), \
                patch.object(login_service.User, "select", return_value=self.user_select), \
                patch.object(login_service.UserTenant, "select", return_value=tenant_query):
            return login_service.authenticate_login(
                identifier,
                password,
                channel=channel,
                password_encrypted=encrypted,
            )

    def test_web_login_accepts_rsa_decrypted_base64_password(self):
        decrypted = base64.b64encode(self.password.encode()).decode()
        with patch("api.utils.decrypt", return_value=decrypted):
            identity = self.auth(
                identifier="alice@example.com",
                password="ciphertext",
                channel="web",
                encrypted=True,
            )
        self.assertEqual(identity.role, "team_owner")
        self.assertEqual(identity.tenant_id, "tenant-1")
        self.assertEqual(identity.user, self.user)

    def test_management_login_accepts_plain_password_for_owner(self):
        identity = self.auth(
            identifier="alice",
            password=self.password,
            channel="management",
            encrypted=False,
        )
        self.assertEqual(identity.role, "team_owner")
        self.assertEqual(identity.tenant_id, "tenant-1")

    def test_non_owner_cannot_enter_management(self):
        with self.assertRaises(login_service.LoginForbidden):
            self.auth(
                identifier="alice",
                password=self.password,
                channel="management",
                encrypted=False,
                owner=False,
            )

    def test_database_failure_is_reported_as_unavailable(self):
        with patch.object(login_service.DB, "connection_context", return_value=nullcontext()), \
                patch.object(login_service.User, "select", side_effect=RuntimeError("db down")):
            with self.assertRaises(login_service.LoginUnavailable):
                login_service.authenticate_login(
                    "alice", self.password, channel="management", password_encrypted=False
                )

    def test_environment_admin_cannot_use_the_web_channel(self):
        with patch.dict(os.environ, {"MANAGEMENT_ADMIN_USERNAME": "admin"}):
            identity = self.auth(
                identifier="admin",
                password=self.password,
                channel="web",
                encrypted=False,
            )
        self.assertNotEqual(identity.role, "admin")
        self.assertEqual(identity.role, "team_owner")

    def test_environment_admin_uses_management_channel_and_never_queries_users(self):
        app = Flask("login-service-admin-test")
        app.config[ADMIN_PASSWORD_ENV] = self.password
        with app.app_context(), \
                patch.dict(os.environ, {"MANAGEMENT_ADMIN_USERNAME": "admin"}), \
                patch.object(login_service.User, "select", side_effect=AssertionError("must not query")):
            identity = login_service.authenticate_login(
                "admin", self.password, channel="management", password_encrypted=False
            )
        self.assertEqual(identity.role, "admin")
        self.assertEqual(identity.user_id, "admin")

    def test_public_identity_never_contains_credentials(self):
        self.user.to_dict = lambda: {
            "id": self.user.id,
            "nickname": self.user.nickname,
            "email": self.user.email,
            "password": self.stored_hash,
            "access_token": "stale-token",
        }
        identity = self.auth(
            identifier="alice",
            password=self.password,
            channel="management",
            encrypted=False,
        )
        data = identity.public_data()
        self.assertNotIn("password", data)
        self.assertNotIn("access_token", data)
        self.assertEqual(data["role"], "team_owner")


if __name__ == "__main__":
    unittest.main()
