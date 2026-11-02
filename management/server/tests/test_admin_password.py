"""Administrator credential and management login regressions."""

import os
import sys
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask

SERVER_ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = SERVER_ROOT.parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

from admin_config import (  # noqa: E402
    ADMIN_PASSWORD_ENV,
    configure_admin_password,
    get_admin_password,
    validate_admin_password,
)
from routes import register_routes  # noqa: E402
from services.auth.permissions import protect_management_api  # noqa: E402

ADMIN_PASSWORD = "test-admin-password-for-regressions"


def build_app():
    app = Flask("management-admin-test")
    app.config["TESTING"] = True
    register_routes(app)
    app.before_request(protect_management_api)
    return app


class AdminPasswordTests(unittest.TestCase):
    def test_missing_blank_short_defaults_and_placeholders_are_rejected(self):
        for password in (
            None, "", " " * 20, "abcdefghijk", "é" * 11, "12345678", "password",
            "12345678" * 2, "admin" * 4, "password" * 2,
            "your-admin-password", "your-admin-password-here", "your_password",
            "change-this-password", "password123456",
            "please-change-this-password", "replace-with-a-strong-password",
            "replace_with_a_unique_password", "default-password", "example-password",
            "placeholder-password", "123456789012", "admin12345678",
        ):
            with self.subTest(password=password), self.assertRaisesRegex(RuntimeError, "MANAGEMENT_ADMIN_PASSWORD"):
                validate_admin_password(password)

    def test_nondefault_passwords_and_passphrases_are_preserved(self):
        for password in (
            "abcdefghijk!", ADMIN_PASSWORD, "lantern orchard river meadow",
            "青山绿水明月清风春花秋实星河", "  lantern orchard river meadow  ",
            "E4" * 100,
        ):
            with self.subTest(password=password):
                self.assertEqual(validate_admin_password(password), password)

    def test_validation_error_does_not_echo_the_password(self):
        with self.assertRaises(RuntimeError) as raised:
            validate_admin_password("your-admin-password")
        self.assertNotIn("your-admin-password", str(raised.exception))

    def test_password_is_app_scoped(self):
        first, second = Flask("first-admin"), Flask("second-admin")
        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: ADMIN_PASSWORD}):
            configure_admin_password(first)
        with patch.dict(os.environ, {ADMIN_PASSWORD_ENV: "another-regression-test-password"}):
            configure_admin_password(second)
            with first.app_context():
                self.assertEqual(get_admin_password(), ADMIN_PASSWORD)
            with second.app_context():
                self.assertEqual(get_admin_password(), "another-regression-test-password")

    def test_admin_login_uses_validated_password_and_never_queries_a_database(self):
        app = build_app()
        app.config[ADMIN_PASSWORD_ENV] = validate_admin_password(ADMIN_PASSWORD)
        with patch.dict(os.environ, {"MANAGEMENT_ADMIN_USERNAME": "admin"}), \
                patch("routes.auth.routes.create_token_session", return_value=("opaque-token", 28800)) as issue, \
                patch("services.users.service.User.select", side_effect=AssertionError("admin login must not query user table")):
            client = app.test_client()
            for password in ("12345678", "your-admin-password", "wrong-regression-password"):
                response = client.post("/api/v1/auth/login", json={"username": "admin", "password": password})
                self.assertEqual(response.status_code, 400)
                self.assertNotIn("data", response.json)

            response = client.post("/api/v1/auth/login", json={"username": "admin", "password": ADMIN_PASSWORD})
            self.assertEqual(response.status_code, 200)
            self.assertEqual(response.json["data"]["token"], "opaque-token")
            self.assertEqual(response.json["data"]["expiresIn"], 28800)
            issue.assert_called_once()

    def test_redis_failure_during_login_returns_503(self):
        app = build_app()
        app.config[ADMIN_PASSWORD_ENV] = validate_admin_password(ADMIN_PASSWORD)
        with patch.dict(os.environ, {"MANAGEMENT_ADMIN_USERNAME": "admin"}), \
                patch("routes.auth.routes.create_token_session", side_effect=RuntimeError("Redis is unavailable")):
            response = app.test_client().post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
        self.assertEqual(response.status_code, 503)

    def test_deployment_uses_one_backend_and_environment_credentials(self):
        for filename in ("docker/docker-compose.yml", "docker/docker-compose_gpu.yml"):
            with self.subTest(filename=filename):
                content = (REPO_ROOT / filename).read_text(encoding="utf-8")
                self.assertIn("env_file: .env", content)
                self.assertNotIn("management-backend", content)


if __name__ == "__main__":
    unittest.main()
