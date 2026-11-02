"""Management administrator credential configuration."""

import os
import re

from flask import current_app

ADMIN_PASSWORD_ENV = "MANAGEMENT_ADMIN_PASSWORD"
MIN_ADMIN_PASSWORD_LENGTH = 12
_KNOWN_ADMIN_DEFAULTS = {
    "12345678", "20250409", "password", "admin", "admin123",
    "password1234", "password12345", "password123456", "password12345678",
}
_ADMIN_PASSWORD_PLACEHOLDERS = {
    "changeme", "changethispassword", "pleasechangethispassword",
    "yourpassword", "youradminpassword", "youradministratorpassword",
    "yourpasswordhere", "youradminpasswordhere", "replacethispassword",
    "replaceme", "replacewithastrongpassword", "replacewithauniquepassword",
    "defaultpassword", "examplepassword", "placeholderpassword",
    "123456789012", "1234567890123456", "admin12345678",
}


def validate_admin_password(password):
    """Require a non-default password; allow long passphrases without composition rules."""
    error = (
        "MANAGEMENT_ADMIN_PASSWORD must be explicitly set to a unique password "
        "of at least 12 characters; default and placeholder values are not allowed. "
        "See docs/security/jwt-secret.md for setup and migration instructions."
    )
    if not isinstance(password, str) or len(password.strip()) < MIN_ADMIN_PASSWORD_LENGTH:
        raise RuntimeError(error)
    normalized = re.sub(r"[^a-z0-9]", "", password.lower())
    if (
        normalized in _ADMIN_PASSWORD_PLACEHOLDERS
        or password.lower() in _KNOWN_ADMIN_DEFAULTS
        or any(password.lower() == default * (len(password) // len(default)) for default in _KNOWN_ADMIN_DEFAULTS)
    ):
        raise RuntimeError(error)
    return password


def configure_admin_password(app):
    """Fail startup before loading routes when administrator credentials are unsafe."""
    app.config[ADMIN_PASSWORD_ENV] = validate_admin_password(os.environ.get(ADMIN_PASSWORD_ENV))


def get_admin_password():
    """Read the startup-validated credential instead of capturing an import-time default."""
    return current_app.config[ADMIN_PASSWORD_ENV]
