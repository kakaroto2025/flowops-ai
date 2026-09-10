from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from tools.email_intake.gmail_oauth import GMAIL_READONLY_SCOPE, GmailOAuthBootstrap, GmailOAuthError


class FakeCredentials:
    def __init__(self, payload: dict | None = None):
        self.payload = payload or {"token": "redacted"}

    @classmethod
    def from_authorized_user_info(cls, payload: dict, scopes: list[str]):
        return cls({"payload": payload, "scopes": scopes})

    def to_json(self) -> str:
        return json.dumps(self.payload)


class FakeFlow:
    calls: list[tuple[str, list[str]]] = []

    @classmethod
    def from_client_secrets_file(cls, path: str, scopes: list[str]):
        cls.calls.append((path, scopes))
        return cls()

    def run_local_server(self, **kwargs):
        self.kwargs = kwargs
        return FakeCredentials()


class FakeKeyring:
    def __init__(self, stored: str | None = None):
        self.stored = stored
        self.set_calls: list[tuple[str, str, str]] = []

    def get_password(self, service: str, username: str):
        return self.stored

    def set_password(self, service: str, username: str, password: str):
        self.set_calls.append((service, username, password))


class FakeGmailService:
    def __init__(self, email: str):
        self.email = email
        self.profile_calls = 0

    def users(self):
        return self

    def getProfile(self, userId: str):
        self.profile_calls += 1
        self.user_id = userId
        return self

    def execute(self):
        return {"emailAddress": self.email}


class GmailOAuthBootstrapTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.client_json = Path(self.tmp.name) / "client_secret.json"
        self.client_json.write_text(json.dumps({"installed": {"client_id": "redacted"}}), encoding="utf-8")
        FakeFlow.calls = []

    def tearDown(self):
        self.tmp.cleanup()

    def bootstrap(self, **overrides) -> GmailOAuthBootstrap:
        service = overrides.pop("service", FakeGmailService("csicriptoinvestigation@gmail.com"))
        return GmailOAuthBootstrap(
            client_json_path=overrides.pop("client_json_path", self.client_json),
            expected_email=overrides.pop("expected_email", "csicriptoinvestigation@gmail.com"),
            scopes=overrides.pop("scopes", (GMAIL_READONLY_SCOPE,)),
            keyring_backend=overrides.pop("keyring_backend", FakeKeyring()),
            flow_factory=overrides.pop("flow_factory", FakeFlow),
            service_builder=overrides.pop("service_builder", lambda *args, **kwargs: service),
            **overrides,
        )

    def test_oauth_bootstrap_uses_exact_gmail_readonly_scope_and_stores_token(self):
        keyring = FakeKeyring()

        result = self.bootstrap(keyring_backend=keyring).authorize_and_verify()

        self.assertTrue(result.mailbox_matches)
        self.assertEqual(result.scopes, (GMAIL_READONLY_SCOPE,))
        self.assertEqual(FakeFlow.calls[0][1], [GMAIL_READONLY_SCOPE])
        self.assertEqual(len(keyring.set_calls), 1)

    def test_existing_keyring_token_skips_browser_flow(self):
        keyring = FakeKeyring(stored=json.dumps({"token": "stored"}))
        bootstrap = self.bootstrap(keyring_backend=keyring)
        bootstrap._credentials_class = lambda: FakeCredentials

        result = bootstrap.authorize_and_verify()

        self.assertTrue(result.mailbox_matches)
        self.assertEqual(FakeFlow.calls, [])

    def test_wrong_scope_is_rejected(self):
        with self.assertRaisesRegex(GmailOAuthError, "gmail.readonly"):
            self.bootstrap(scopes=("https://www.googleapis.com/auth/gmail.modify",)).authorize_and_verify()

    def test_web_client_json_is_rejected(self):
        self.client_json.write_text(json.dumps({"web": {"client_id": "redacted"}}), encoding="utf-8")

        with self.assertRaisesRegex(GmailOAuthError, "Desktop App"):
            self.bootstrap().authorize_and_verify()

    def test_mailbox_mismatch_is_blocked_before_token_storage(self):
        keyring = FakeKeyring()

        with self.assertRaisesRegex(GmailOAuthError, "does not match"):
            self.bootstrap(keyring_backend=keyring, service=FakeGmailService("other@example.com")).authorize_and_verify()

        self.assertEqual(keyring.set_calls, [])

    def test_missing_client_json_is_controlled(self):
        with self.assertRaisesRegex(GmailOAuthError, "not found"):
            self.bootstrap(client_json_path=Path(self.tmp.name) / "missing.json").authorize_and_verify()


if __name__ == "__main__":
    unittest.main()
