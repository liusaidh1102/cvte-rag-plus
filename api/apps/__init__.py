#
#  Copyright 2024 The InfiniFlow Authors. All Rights Reserved.
#
#  Licensed under the Apache License, Version 2.0 (the "License");
#  you may not use this file except in compliance with the License.
#  You may obtain a copy of the License at
#
#      http://www.apache.org/licenses/LICENSE-2.0
#
#  Unless required by applicable law or agreed to in writing, software
#  distributed under the License is distributed on an "AS IS" BASIS,
#  WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
#  See the License for the specific language governing permissions and
#  limitations under the License.
#
import os
import sys
from importlib.util import module_from_spec, spec_from_file_location
from pathlib import Path
from flask import Blueprint, Flask, request
from werkzeug.wrappers.request import Request
from flask_cors import CORS
from flasgger import Swagger
from dotenv import load_dotenv

from api.db import StatusEnum
from api.db.db_models import close_connection
from api.db.services import UserService
from api.utils import CustomJSONEncoder, commands
from api.auth.token_session import extract_bearer_token, get_token_session

from flask_session import Session
from flask_login import LoginManager
from api.utils.api_utils import server_error_response
from api.constants import API_VERSION

__all__ = ["app"]

# Local development reads the same private settings as Docker Compose. Existing
# process environment variables (including Docker env_file) take precedence.
load_dotenv(Path(__file__).resolve().parents[2] / "docker" / ".env")

_MANAGEMENT_UPLOAD_ENDPOINTS = {
    "file_upload.init_upload",
    "file_upload.get_presigned_part",
    "file_upload.list_missing_chunk",
    "file_upload.merge",
}


class _ManagementAdminUser:
    """Minimal identity for the environment-configured management admin.

    The admin has no ``user`` row, so it is only exposed to the four upload
    endpoints. Those endpoints resolve the owning user from the target
    knowledge base before writing any resource.
    """

    id = "admin"
    nickname = "admin"
    email = ""
    is_authenticated = True
    is_active = True
    is_anonymous = False
    is_superuser = True

    def get_id(self):
        return self.id

Request.json = property(lambda self: self.get_json(force=True, silent=True))

app = Flask(__name__)

# Add this at the beginning of your file to configure Swagger UI
swagger_config = {
    "headers": [],
    "specs": [
        {
            "endpoint": "apispec",
            "route": "/apispec.json",
            "rule_filter": lambda rule: True,  # Include all endpoints
            "model_filter": lambda tag: True,  # Include all models
        }
    ],
    "static_url_path": "/flasgger_static",
    "swagger_ui": True,
    "specs_route": "/apidocs/",
}
# 访问 /apidocs/ 查看接口文档
swagger = Swagger(
    app,
    config=swagger_config,
    template={
        "swagger": "2.0",
        "info": {
            "title": "CVTE-RAG-Plus API",
            "description": "",
            "version": "1.0.0",
        },
        "securityDefinitions": {
            "ApiKeyAuth": {"type": "apiKey", "name": "Authorization", "in": "header"}
        },
    },
)

CORS(app, supports_credentials=True, max_age=2592000)
app.url_map.strict_slashes = False
app.json_encoder = CustomJSONEncoder
# 把 Exception 和 处理函数 server_error_response 存入  self.error_handler_spec 字典。
app.errorhandler(Exception)(server_error_response)

## convince for dev and debug
# app.config["LOGIN_DISABLED"] = True
app.config["SESSION_PERMANENT"] = False
app.config["SESSION_TYPE"] = "filesystem"
app.config["MAX_CONTENT_LENGTH"] = int(
    os.environ.get("MAX_CONTENT_LENGTH", 128 * 1024 * 1024)
)

Session(app)
login_manager = LoginManager()
login_manager.init_app(app)

commands.register_commands(app)


def search_pages_path(pages_dir):
    app_path_list = [
        path for path in pages_dir.glob("*_app.py") if not path.name.startswith(".")
    ]
    api_path_list = [
        path for path in pages_dir.glob("*sdk/*.py") if not path.name.startswith(".")
    ]
    app_path_list.extend(api_path_list)
    return app_path_list


def register_page(page_path):
    path = f"{page_path}"

    page_name = page_path.stem.rstrip("_app")
    module_name = ".".join(
        page_path.parts[page_path.parts.index("api"): -1] + (page_name,)
    )

    spec = spec_from_file_location(module_name, page_path)
    page = module_from_spec(spec)
    page.app = app
    page.manager = Blueprint(page_name, module_name)
    sys.modules[module_name] = page
    spec.loader.exec_module(page)
    page_name = getattr(page, "page_name", page_name)
    sdk_path = "\\sdk\\" if sys.platform.startswith("win") else "/sdk/"
    url_prefix = (
        f"/api/{API_VERSION}" if sdk_path in path else f"/{API_VERSION}/{page_name}"
    )

    app.register_blueprint(page.manager, url_prefix=url_prefix)
    return url_prefix


pages_dir = [
    Path(__file__).parent,
    Path(__file__).parent.parent / "api" / "apps",
    Path(__file__).parent.parent / "api" / "apps" / "sdk",
]

client_urls_prefix = [
    register_page(path) for dir in pages_dir for path in search_pages_path(dir)
]

# Management routes are part of the same Flask application and keep their
# existing /api/v1 prefix so the management frontend does not need URL changes.
_MANAGEMENT_SERVER_DIR = Path(__file__).resolve().parents[2] / "management" / "server"
if str(_MANAGEMENT_SERVER_DIR) not in sys.path:
    sys.path.insert(0, str(_MANAGEMENT_SERVER_DIR))

from routes import register_routes as register_management_routes  # noqa: E402
from admin_config import configure_admin_password  # noqa: E402
from services.auth.permissions import protect_management_api  # noqa: E402

configure_admin_password(app)
register_management_routes(app)
app.before_request(protect_management_api)


@login_manager.request_loader
def load_user(web_request):
    authorization = web_request.headers.get("Authorization")
    if not authorization:
        return None

    token = extract_bearer_token(authorization)
    session = get_token_session(token)
    if session:
        # The environment-configured management super admin has no row in the
        # user table and must not inherit access to end-user APIs. A database
        # superuser uses its real user id and remains a valid end-user account.
        if session.get("user_id") == "admin":
            if (
                session.get("role") == "admin"
                and request.endpoint in _MANAGEMENT_UPLOAD_ENDPOINTS
            ):
                return _ManagementAdminUser()
            return None
        user = UserService.query(
            id=session["user_id"], status=StatusEnum.VALID.value
        )
        return user[0] if user else None

    return None


@app.teardown_request
def _db_close(exc):
    close_connection()
