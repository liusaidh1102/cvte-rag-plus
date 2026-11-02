"""Management API authorization regressions with real Flask routes.

Only backing services/database calls are mocked; no live infrastructure is used.
Tokens are opaque values resolved by the same Redis-session contract used in
production, with ``get_token_session`` replaced by an in-memory token map.
"""

import importlib
import re
import sys
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import Mock, patch

from flask import Flask, jsonify
from api.auth.login_service import LoginError, LoginForbidden, LoginUnavailable

SERVER_ROOT = Path(__file__).resolve().parents[1]
if str(SERVER_ROOT) not in sys.path:
    sys.path.insert(0, str(SERVER_ROOT))

ADMIN = {"id": "admin", "username": "admin", "role": "admin", "tenant_id": None}
OWNER = {"id": "owner-id", "username": "owner", "role": "team_owner", "tenant_id": "own-tenant"}


class FakeIdentity:
    def __init__(self, role="user", tenant_id="tenant-1"):
        self.user_id = "user-1"
        self.username = "alice"
        self.email = "alice@example.com"
        self.role = role
        self.tenant_id = tenant_id
        self.is_superuser = role == "admin"

    def session_claims(self):
        return {
            "user_id": self.user_id,
            "username": self.username,
            "email": self.email,
            "role": self.role,
            "tenant_id": self.tenant_id,
            "is_superuser": self.is_superuser,
        }

    def public_data(self):
        return {
            "id": self.user_id,
            "username": self.username,
            "nickname": self.username,
            "email": self.email,
            "role": self.role,
            "tenant_id": self.tenant_id,
        }

SERVICE_FUNCTIONS = {
    "users": [
        "authenticate_user", "get_users_with_pagination", "delete_user",
        "create_user", "update_user", "reset_user_password", "get_user_info_by_id",
    ],
    "teams": [
        "get_teams_with_pagination", "get_team_by_id", "delete_team",
        "get_team_members", "add_team_member", "remove_team_member",
    ],
    "tenants": ["get_tenants_with_pagination", "update_tenant"],
    "conversation": ["get_conversations_by_user_id", "get_messages_by_conversation_id"],
    "files": [
        "batch_delete_files", "check_upload_status", "delete_file",
        "download_file_from_minio", "get_file_info", "get_files_list",
        "handle_chunk_upload", "merge_chunks", "upload_files_to_server",
    ],
}
KB_FUNCTIONS = [
    "get_knowledgebase_list", "get_knowledgebase_detail", "create_knowledgebase",
    "update_knowledgebase", "delete_knowledgebase", "batch_delete_knowledgebase",
    "get_knowledgebase_documents", "add_documents_to_knowledgebase", "delete_document",
    "get_document_parse_progress", "parse_document", "get_system_embedding_config",
    "set_system_embedding_config", "start_sequential_batch_parse_async",
    "get_sequential_batch_parse_progress", "get_tenant_embedding", "get_kb_embedding_config",
]


def module_stub(name, **attributes):
    module = types.ModuleType(name)
    module.__dict__.update(attributes)
    return module


class TokenStore:
    def __init__(self):
        self.tokens = {}

    def add(self, payload):
        token = f"opaque-{len(self.tokens) + 1}"
        claims = dict(payload)
        if "user_id" not in claims and "id" in claims:
            claims["user_id"] = claims["id"]
        self.tokens[token] = claims
        return token

    def get(self, token):
        return self.tokens.get(token)

    def delete(self, token):
        self.tokens.pop(token, None)


@contextmanager
def management_app():
    services = {}
    modules = {}
    for service, functions in SERVICE_FUNCTIONS.items():
        module = module_stub(f"services.{service}.service")
        for name in functions:
            setattr(module, name, Mock(name=name, return_value=True))
        services[service] = module
        modules[module.__name__] = module

    kb = types.SimpleNamespace(**{name: Mock(name=name, return_value={}) for name in KB_FUNCTIONS})
    services["knowledgebases"] = kb
    modules["services.knowledgebases.service"] = module_stub(
        "services.knowledgebases.service", KnowledgebaseService=kb
    )
    modules["services.files.utils"] = module_stub(
        "services.files.utils",
        FileType=types.SimpleNamespace(FOLDER=types.SimpleNamespace(value="folder")),
    )
    modules["utils"] = module_stub(
        "utils",
        success_response=lambda data=None, message="成功", code=0: jsonify(code=code, data=data, message=message),
        error_response=lambda message="失败", code=500, **kwargs: (jsonify(code=code, message=message), code),
    )

    services["users"].authenticate_user.return_value = (
        True,
        {"id": ADMIN["id"], "username": ADMIN["username"], "role": ADMIN["role"], "tenant_id": None, "is_superuser": True},
        None,
    )
    services["users"].get_user_info_by_id.return_value = dict(ADMIN, roles=["admin"])
    for service, function in (
        ("users", "get_users_with_pagination"), ("teams", "get_teams_with_pagination"),
        ("tenants", "get_tenants_with_pagination"), ("files", "get_files_list"),
        ("conversation", "get_conversations_by_user_id"),
        ("conversation", "get_messages_by_conversation_id"),
    ):
        getattr(services[service], function).return_value = ([], 0)
    services["teams"].get_team_by_id.return_value = {"id": "own-tenant"}
    services["teams"].get_team_members.return_value = []
    services["files"].get_file_info.return_value = {
        "id": "own-file", "type": "pdf", "parent_id": "bucket", "location": "report.pdf",
    }
    services["files"].download_file_from_minio.return_value = (b"private report", "report.pdf")
    services["files"].upload_files_to_server.return_value = {"data": []}
    services["files"].handle_chunk_upload.return_value = {"code": 0}
    services["files"].merge_chunks.return_value = {"code": 0}
    services["files"].batch_delete_files.return_value = 1
    services["files"].check_upload_status.return_value = {"code": 0, "data": {"is_complete": False}}
    kb.get_knowledgebase_detail.return_value = {"id": "own-kb"}
    kb.update_knowledgebase.return_value = {"id": "own-kb"}
    kb.create_knowledgebase.return_value = {"id": "new-kb"}
    kb.parse_document.return_value = {"success": True}
    kb.start_sequential_batch_parse_async.return_value = {"success": True}
    kb.set_system_embedding_config.return_value = (True, "saved")
    kb.get_knowledgebase_list.return_value = ([], 0)
    kb.get_knowledgebase_documents.return_value = ([], 0)
    kb.batch_delete_knowledgebase.return_value = 1

    with patch.dict(sys.modules, modules):
        for name in list(sys.modules):
            if name == "routes" or name.startswith("routes."):
                del sys.modules[name]
        routes = importlib.import_module("routes")
        permissions = importlib.import_module("services.auth.permissions")
        auth_utils = importlib.import_module("services.auth.auth_utils")
        auth_routes = importlib.import_module("routes.auth.routes")

        tokens = TokenStore()
        with patch.object(auth_utils, "get_token_session", side_effect=tokens.get), \
                patch.object(auth_routes, "create_token_session", side_effect=lambda claims, ttl=None: (tokens.add(claims), 28800)), \
                patch.object(auth_routes, "delete_token_session", side_effect=tokens.delete):
            app = Flask("management-auth-test")
            app.config["TESTING"] = True
            routes.register_routes(app)
            app.before_request(permissions.protect_management_api)
            yield app, services, permissions, tokens


def headers(token):
    return {"Authorization": f"Bearer {token}"}


def mock_resource_rows(query, params):
    if query.startswith("SELECT f2d.file_id"):
        return []
    if query.startswith("SELECT role FROM user_tenant"):
        return [{"role": "owner"}] if params[1] == "another-owner" else []
    if query.startswith("SELECT llm_name FROM tenant_llm"):
        return [{"llm_name": "own-model___fixture"}] if params[0] == OWNER["tenant_id"] else []
    expected = "own-file" if "FROM file WHERE" in query else "own-doc" if "FROM document d" in query else "own-kb"
    scope = OWNER["id"] if expected == "own-file" else OWNER["tenant_id"]
    return [{"id": resource_id} for resource_id in params[1:] if resource_id == expected and params[0] == scope]


class ManagementAuthenticationTests(unittest.TestCase):
    def test_every_registered_api_method_requires_auth_before_service_calls(self):
        with management_app() as (app, services, permissions, _), patch.object(permissions, "_query_rows") as query:
            client = app.test_client()
            count = 0
            for rule in app.url_map.iter_rules():
                if not rule.rule.startswith("/api/"):
                    continue
                path = re.sub(r"<[^>]+>", "target", rule.rule)
                for method in rule.methods - {"OPTIONS", "HEAD"}:
                    if rule.rule == "/api/v1/auth/login" and method == "POST":
                        continue
                    with self.subTest(path=path, method=method):
                        response = client.open(path, method=method, json={})
                        self.assertEqual(response.status_code, 401)
                        self.assertEqual(response.headers["WWW-Authenticate"], "Bearer")
                        count += 1
            self.assertGreater(count, 40)
            query.assert_not_called()
            for service in services.values():
                for item in vars(service).values():
                    if isinstance(item, Mock):
                        item.assert_not_called()

    def test_login_is_public_and_issues_an_opaque_token(self):
        with management_app() as (app, services, _, tokens):
            client = app.test_client()
            auth_routes = sys.modules["routes.auth.routes"]

            class Identity:
                role = "admin"
                username = "admin"
                tenant_id = None

                def session_claims(self):
                    return {
                        "user_id": "admin",
                        "username": "admin",
                        "role": "admin",
                        "tenant_id": None,
                        "is_superuser": True,
                    }

                def public_data(self):
                    return {"id": "admin", "username": "admin", "nickname": "admin", "role": "admin"}

            with patch.object(auth_routes, "authenticate_login", return_value=Identity()) as authenticate:
                response = client.post(
                    "/api/v1/auth/login",
                    json={"username": "admin", "password": "fixture", "channel": "management"},
                )
            self.assertEqual(response.status_code, 200)
            token = response.json["data"]["token"]
            self.assertEqual(tokens.get(token)["role"], "admin")
            self.assertEqual(response.json["data"]["access_token"], token)
            self.assertEqual(response.headers["Authorization"], f"Bearer {token}")
            authenticate.assert_called_once_with(
                "admin", "fixture", channel="management", password_encrypted=None
            )
            self.assertEqual(client.get("/api/v1/auth/login").status_code, 405)

    def test_web_login_uses_the_same_endpoint_and_shared_authenticator(self):
        with management_app() as (app, _, _, tokens):
            client = app.test_client()
            auth_routes = sys.modules["routes.auth.routes"]
            identity = FakeIdentity()
            with patch.object(auth_routes, "authenticate_login", return_value=identity) as authenticate:
                response = client.post(
                    "/api/v1/auth/login",
                    json={
                        "email": "alice@example.com",
                        "password": "ciphertext",
                        "channel": "web",
                        "password_encrypted": "true",
                    },
                )
            self.assertEqual(response.status_code, 200)
            authenticate.assert_called_once_with(
                "alice@example.com", "ciphertext", channel="web", password_encrypted=True
            )
            token = response.json["data"]["token"]
            self.assertEqual(tokens.get(token)["role"], "user")
            self.assertEqual(response.json["data"]["access_token"], token)
            self.assertEqual(response.json["data"]["channel"], "web")
            self.assertEqual(response.headers["Authorization"], f"Bearer {token}")

    def test_login_maps_forbidden_unavailable_and_web_errors(self):
        with management_app() as (app, _, _, _):
            client = app.test_client()
            auth_routes = sys.modules["routes.auth.routes"]
            with patch.object(auth_routes, "authenticate_login", side_effect=LoginForbidden("no access")):
                response = client.post(
                    "/api/v1/auth/login",
                    json={"username": "alice", "password": "plain", "channel": "management"},
                )
            self.assertEqual(response.status_code, 403)
            self.assertEqual(response.json["code"], 403)

            with patch.object(auth_routes, "authenticate_login", side_effect=LoginUnavailable("down")):
                response = client.post(
                    "/api/v1/auth/login",
                    json={"email": "alice@example.com", "password": "plain", "channel": "web"},
                )
            self.assertEqual(response.status_code, 503)

            with patch.object(auth_routes, "authenticate_login", side_effect=LoginError("bad credentials")):
                web_response = client.post(
                    "/api/v1/auth/login",
                    json={"email": "alice@example.com", "password": "bad", "channel": "web"},
                )
                admin_response = client.post(
                    "/api/v1/auth/login",
                    json={"username": "alice", "password": "bad", "channel": "management"},
                )
            self.assertEqual(web_response.status_code, 200)
            self.assertEqual(web_response.json["code"], 109)
            self.assertEqual(admin_response.status_code, 400)
            self.assertEqual(admin_response.json["code"], 1)

    def test_every_preflight_is_public_and_never_runs_backend_work(self):
        with management_app() as (app, services, permissions, _), patch.object(permissions, "_query_rows") as query:
            client = app.test_client()
            for rule in app.url_map.iter_rules():
                if not rule.rule.startswith("/api/"):
                    continue
                path = re.sub(r"<[^>]+>", "target", rule.rule)
                method = next(iter(rule.methods - {"OPTIONS", "HEAD"}))
                with self.subTest(path=path):
                    self.assertEqual(client.options(path).status_code, 200)
            query.assert_not_called()
            for service in services.values():
                for item in vars(service).values():
                    if isinstance(item, Mock):
                        item.assert_not_called()

    def test_invalid_and_incomplete_tokens_are_rejected(self):
        with management_app() as (app, services, _, tokens):
            client = app.test_client()
            bad_payloads = [
                {}, {"user_id": "", "username": "x", "role": "admin"},
                {"user_id": "x", "username": "", "role": "admin"},
                {"user_id": "x", "username": "x", "role": "user"},
                {"user_id": "x", "username": "x", "role": "team_owner", "tenant_id": None},
            ]
            tokens.tokens.update({f"bad-{index}": payload for index, payload in enumerate(bad_payloads)})
            for token in ["not-a-token", *tokens.tokens]:
                with self.subTest(token=token):
                    self.assertEqual(client.get("/api/v1/users", headers=headers(token)).status_code, 401)
            for value in ("", "Basic abc", "Bearer", "Bearer token extra"):
                self.assertEqual(client.get("/api/v1/users", headers={"Authorization": value}).status_code, 401)
            services["users"].get_users_with_pagination.assert_not_called()

    def test_authenticated_admin_retains_global_access(self):
        with management_app() as (app, services, permissions, tokens), patch.object(permissions, "_query_rows") as query:
            client = app.test_client()
            token = tokens.add(ADMIN)
            self.assertEqual(client.get("/api/v1/users", headers=headers(token)).status_code, 200)
            self.assertEqual(client.get("/api/v1/tenants", headers=headers(token)).status_code, 200)
            self.assertEqual(client.delete("/api/v1/files/own-file", headers=headers(token)).status_code, 200)
            self.assertEqual(client.get("/api/v1/knowledgebases/own-kb", headers=headers(token)).status_code, 200)
            query.assert_not_called()
            services["files"].delete_file.assert_called_once()

    def test_owner_can_only_touch_own_resources(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=mock_resource_rows
        ):
            client = app.test_client()
            token = tokens.add(OWNER)
            self.assertEqual(client.get("/api/v1/files/own-file/download", headers=headers(token)).status_code, 200)
            self.assertEqual(client.get("/api/v1/files/foreign-file/download", headers=headers(token)).status_code, 403)
            self.assertEqual(client.delete("/api/v1/files/own-file", headers=headers(token)).status_code, 200)
            self.assertEqual(client.delete("/api/v1/files/foreign-file", headers=headers(token)).status_code, 403)
            self.assertEqual(client.get("/api/v1/knowledgebases/own-kb", headers=headers(token)).status_code, 200)
            self.assertEqual(client.get("/api/v1/knowledgebases/foreign-kb", headers=headers(token)).status_code, 403)
            self.assertEqual(client.delete("/api/v1/knowledgebases/foreign-kb", headers=headers(token)).status_code, 403)

    def test_batch_operations_require_every_id_to_belong_to_the_owner(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=mock_resource_rows
        ):
            client = app.test_client()
            token = tokens.add(OWNER)
            self.assertEqual(
                client.delete("/api/v1/files/batch", json={"ids": ["own-file"]}, headers=headers(token)).status_code,
                200,
            )
            self.assertEqual(
                client.delete("/api/v1/files/batch", json={"ids": ["own-file", "foreign-file"]}, headers=headers(token)).status_code,
                403,
            )
            self.assertEqual(
                client.delete("/api/v1/knowledgebases/batch", json={"ids": ["own-kb"]}, headers=headers(token)).status_code,
                200,
            )
            self.assertEqual(
                client.delete(
                    "/api/v1/knowledgebases/batch",
                    json={"ids": ["own-kb", "foreign-kb"]},
                    headers=headers(token),
                ).status_code,
                403,
            )

    def test_owner_cannot_forge_ownership_fields_or_select_another_tenant_model(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=mock_resource_rows
        ):
            client = app.test_client()
            token = tokens.add(OWNER)
            for field in ("tenant_id", "created_by", "creator_id"):
                with self.subTest(field=field):
                    response = client.post(
                        "/api/v1/knowledgebases",
                        json={"name": "new", "embd_id": "own-model", field: "foreign"},
                        headers=headers(token),
                    )
                    self.assertEqual(response.status_code, 403)
            services["knowledgebases"].create_knowledgebase.assert_not_called()

            response = client.post(
                "/api/v1/knowledgebases",
                json={"name": "new", "embd_id": "own-model"},
                headers=headers(token),
            )
            self.assertEqual(response.status_code, 200)
            kwargs = services["knowledgebases"].create_knowledgebase.call_args.kwargs
            self.assertEqual(kwargs["creator_id"], OWNER["tenant_id"])
            self.assertEqual(kwargs["created_by"], OWNER["id"])
            self.assertEqual(kwargs["embd_id"], "own-model___fixture")

    def test_team_membership_cannot_grant_management_or_demote_owner(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=mock_resource_rows
        ):
            client = app.test_client()
            token = tokens.add(OWNER)
            self.assertEqual(client.get("/api/v1/teams/foreign-tenant", headers=headers(token)).status_code, 403)
            self.assertEqual(
                client.post(
                    "/api/v1/teams/own-tenant/members",
                    json={"userId": "member-1", "role": "admin"},
                    headers=headers(token),
                ).status_code,
                403,
            )
            self.assertEqual(
                client.post(
                    "/api/v1/teams/own-tenant/members",
                    json={"userId": "member-1", "role": "normal"},
                    headers=headers(token),
                ).status_code,
                200,
            )
            self.assertEqual(
                client.delete("/api/v1/teams/own-tenant/members/another-owner", headers=headers(token)).status_code,
                403,
            )
            self.assertEqual(
                client.delete("/api/v1/teams/own-tenant/members/member-1", headers=headers(token)).status_code,
                200,
            )

    def test_owner_global_admin_endpoints_and_new_routes_fail_closed(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=mock_resource_rows
        ):
            app.add_url_rule(
                "/api/v1/future",
                endpoint="knowledgebases.future_route",
                view_func=lambda: jsonify(code=0),
                methods=["GET"],
            )
            client = app.test_client()
            token = tokens.add(OWNER)
            for path in ("/api/v1/users", "/api/v1/tenants", "/api/v1/conversation", "/api/v1/future"):
                with self.subTest(path=path):
                    self.assertEqual(client.get(path, headers=headers(token)).status_code, 403)
            self.assertEqual(client.post("/api/v1/users", json={}, headers=headers(token)).status_code, 403)
            services["users"].get_users_with_pagination.assert_not_called()
            services["tenants"].get_tenants_with_pagination.assert_not_called()

    def test_owner_upload_status_and_logout_remain_available(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=mock_resource_rows
        ):
            client = app.test_client()
            token = tokens.add(OWNER)
            self.assertEqual(
                client.get("/api/v1/files/upload/status?uploadId=x&fileName=a&totalChunks=1", headers=headers(token)).status_code,
                200,
            )
            self.assertEqual(client.post("/api/v1/auth/logout", headers=headers(token)).status_code, 200)
            self.assertIsNone(tokens.get(token))

    def test_authorization_backend_failure_returns_503(self):
        with management_app() as (app, services, permissions, tokens), patch.object(
            permissions, "_query_rows", side_effect=RuntimeError("database unavailable")
        ):
            client = app.test_client()
            token = tokens.add(OWNER)
            response = client.get("/api/v1/files/own-file/download", headers=headers(token))
            self.assertEqual(response.status_code, 503)
            services["files"].download_file_from_minio.assert_not_called()


if __name__ == "__main__":
    unittest.main()
