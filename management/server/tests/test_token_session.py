"""Opaque Redis token session regressions (no JWT involved)."""

import hashlib
import json
import os
import sys
import time
import unittest
from pathlib import Path
from unittest.mock import patch

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from api.auth import token_session  # noqa: E402


class FakeRedis:
    def __init__(self):
        self.values = {}
        self.ttls = {}
        self.available = True

    def set_obj(self, key, obj, exp):
        if not self.available:
            return False
        self.values[key] = json.dumps(obj)
        self.ttls[key] = exp
        return True

    def get(self, key):
        return self.values.get(key)

    def delete(self, key):
        self.values.pop(key, None)
        self.ttls.pop(key, None)
        return 1


class TokenSessionTests(unittest.TestCase):
    def setUp(self):
        self.redis = FakeRedis()
        self.patcher = patch.object(token_session, "_redis", return_value=self.redis)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def claims(self, **changes):
        payload = {
            "user_id": "user-1",
            "username": "alice",
            "email": "alice@example.com",
            "role": "team_owner",
            "tenant_id": "tenant-1",
            "is_superuser": False,
            "login_channel": "password",
        }
        payload.update(changes)
        return payload

    def test_token_is_random_and_only_its_digest_is_stored(self):
        token, expires_in = token_session.create_token_session(self.claims(), ttl=120)
        self.assertGreaterEqual(len(token), 40)
        key = "cvte-rag-plus:auth:session:" + hashlib.sha256(token.encode()).hexdigest()
        self.assertIn(key, self.redis.values)
        self.assertNotIn(token, self.redis.values)
        self.assertEqual(expires_in, 120)
        self.assertEqual(self.redis.ttls[key], 120)
        self.assertEqual(token_session.get_token_session(token)["user_id"], "user-1")

    def test_ttl_environment_is_read_lazily(self):
        with patch.dict(os.environ, {"AUTH_TOKEN_TTL_SECONDS": "123"}):
            _, expires_in = token_session.create_token_session(self.claims())
        self.assertEqual(expires_in, 123)

    def test_delete_revokes_the_session_immediately(self):
        token, _ = token_session.create_token_session(self.claims())
        token_session.delete_token_session(token)
        self.assertIsNone(token_session.get_token_session(token))

    def test_expired_or_malformed_sessions_are_rejected_and_removed(self):
        token, _ = token_session.create_token_session(self.claims())
        key = "cvte-rag-plus:auth:session:" + hashlib.sha256(token.encode()).hexdigest()
        expired_payload = json.loads(self.redis.values[key])
        expired_payload["expires_at"] = int(time.time()) - 1
        self.redis.values[key] = json.dumps(expired_payload)
        self.assertIsNone(token_session.get_token_session(token))
        self.assertNotIn(key, self.redis.values)

        malformed_token, _ = token_session.create_token_session(self.claims())
        malformed_key = "cvte-rag-plus:auth:session:" + hashlib.sha256(malformed_token.encode()).hexdigest()
        self.redis.values[malformed_key] = "not-json"
        self.assertIsNone(token_session.get_token_session(malformed_token))

        missing_user, _ = token_session.create_token_session(self.claims(user_id=""))
        self.assertIsNone(token_session.get_token_session(missing_user))

    def test_redis_outage_refuses_to_issue_a_session(self):
        self.redis.available = False
        with self.assertRaisesRegex(RuntimeError, "Redis is unavailable"):
            token_session.create_token_session(self.claims())

    def test_bearer_and_legacy_raw_headers_are_accepted(self):
        for value, expected in (
            ("Bearer abc", "abc"),
            ("bearer abc", "abc"),
            ("abc", "abc"),
            ("Bearer", None),
            ("", None),
            (None, None),
            ("Bearer a b", None),
        ):
            with self.subTest(value=value):
                self.assertEqual(token_session.extract_bearer_token(value), expected)


if __name__ == "__main__":
    unittest.main()
