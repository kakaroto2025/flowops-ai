from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any


GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
GMAIL_MODIFY_SCOPE = "https://www.googleapis.com/auth/gmail.modify"
GMAIL_ALLOWED_SCOPES = (GMAIL_MODIFY_SCOPE,)
GMAIL_UNEXPECTED_BROAD_SCOPES = ("https://mail.google.com/",)
TOKEN_SERVICE_NAME = "FlowOps AI Pilot v2 Gmail OAuth"


class GmailOAuthError(RuntimeError):
    pass


@dataclass(frozen=True)
class GmailOAuthResult:
    authorized_email: str
    expected_email: str
    scopes: tuple[str, ...]
    granted_scopes: tuple[str, ...]
    token_stored: bool

    @property
    def mailbox_matches(self) -> bool:
        return self.authorized_email.lower() == self.expected_email.lower()


class GmailOAuthBootstrap:
    def __init__(
        self,
        *,
        client_json_path: str | Path,
        expected_email: str,
        scopes: tuple[str, ...] = (GMAIL_READONLY_SCOPE,),
        token_service_name: str = TOKEN_SERVICE_NAME,
        keyring_backend: Any | None = None,
        flow_factory: Any | None = None,
        service_builder: Any | None = None,
    ):
        self.client_json_path = Path(client_json_path)
        self.expected_email = expected_email.strip().lower()
        self.scopes = tuple(scopes)
        self.token_service_name = token_service_name
        self.keyring = keyring_backend
        self.flow_factory = flow_factory
        self.service_builder = service_builder

    def authorize_and_verify(self) -> GmailOAuthResult:
        self._validate_scope()
        self._validate_client_json_path()
        credentials = self._load_stored_credentials()
        if credentials is None:
            credentials = self._run_local_oauth_flow()
        service = self._build_gmail_service(credentials)
        authorized_email = self._verify_profile_email(service)
        if authorized_email.lower() != self.expected_email:
            raise GmailOAuthError("Authorized Gmail mailbox does not match the expected controlled account.")
        self._store_credentials(credentials)
        return GmailOAuthResult(
            authorized_email=authorized_email,
            expected_email=self.expected_email,
            scopes=self.scopes,
            granted_scopes=self._granted_scopes(credentials),
            token_stored=True,
        )

    def _validate_scope(self) -> None:
        if self.scopes != GMAIL_ALLOWED_SCOPES:
            raise GmailOAuthError("Gmail OAuth bootstrap requires the exact gmail.modify scope.")

    def _validate_client_json_path(self) -> None:
        if not self.client_json_path.exists():
            raise GmailOAuthError("OAuth Desktop client JSON was not found.")
        try:
            payload = json.loads(self.client_json_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise GmailOAuthError("OAuth Desktop client JSON is not readable.") from exc
        if "installed" not in payload:
            raise GmailOAuthError("OAuth client JSON must be a Desktop App credential.")

    def _load_stored_credentials(self):
        raw = self._keyring().get_password(self.token_service_name, self.expected_email)
        if not raw:
            return None
        try:
            payload = json.loads(raw)
            if self._payload_has_unexpected_broad_scope(payload):
                raise GmailOAuthError("Stored Gmail OAuth credential includes an unexpectedly broad Gmail scope.")
            if not self._payload_grants_required_scope(payload):
                return None
            credentials_class = self._credentials_class()
            credentials = credentials_class.from_authorized_user_info(payload, list(self.scopes))
            self._validate_granted_scopes(credentials)
            return credentials
        except Exception as exc:
            raise GmailOAuthError("Stored Gmail OAuth credentials are invalid.") from exc

    def _run_local_oauth_flow(self):
        try:
            flow_class = self.flow_factory or self._installed_app_flow_class()
            flow = flow_class.from_client_secrets_file(str(self.client_json_path), list(self.scopes))
            credentials = flow.run_local_server(
                port=0,
                open_browser=True,
                prompt="consent",
                authorization_prompt_message="Complete Gmail OAuth authorization in the browser window.",
                success_message="FlowOps Gmail OAuth authorization completed. You may close this tab.",
            )
            self._validate_granted_scopes(credentials)
            return credentials
        except Exception as exc:
            raise GmailOAuthError("Gmail OAuth browser authorization failed.") from exc

    def _build_gmail_service(self, credentials):
        try:
            builder = self.service_builder or self._google_service_builder()
            return builder("gmail", "v1", credentials=credentials)
        except Exception as exc:
            raise GmailOAuthError("Authenticated Gmail service could not be created.") from exc

    def _verify_profile_email(self, service) -> str:
        try:
            profile = service.users().getProfile(userId="me").execute()
        except Exception as exc:
            raise GmailOAuthError("Gmail profile verification failed.") from exc
        email = str(profile.get("emailAddress") or "").strip().lower()
        if not email:
            raise GmailOAuthError("Gmail profile response did not include an email address.")
        return email

    def _store_credentials(self, credentials) -> None:
        try:
            payload = json.loads(credentials.to_json())
            payload["flowops_granted_scopes"] = list(self._granted_scopes(credentials))
            self._keyring().set_password(self.token_service_name, self.expected_email, json.dumps(payload))
        except Exception as exc:
            raise GmailOAuthError("Gmail OAuth token could not be stored in the OS credential store.") from exc

    def _payload_grants_required_scope(self, payload: dict[str, Any]) -> bool:
        granted = self._scopes_from_payload(payload)
        return bool(granted) and self._scopes_are_allowed(granted)

    def _payload_has_unexpected_broad_scope(self, payload: dict[str, Any]) -> bool:
        granted = set(self._scopes_from_payload(payload))
        return any(scope in granted for scope in GMAIL_UNEXPECTED_BROAD_SCOPES)

    def _validate_granted_scopes(self, credentials) -> None:
        granted = self._granted_scopes(credentials)
        if not granted:
            raise GmailOAuthError("Gmail OAuth credential did not expose granted scopes safely.")
        if not self._scopes_are_allowed(granted):
            raise GmailOAuthError("Gmail OAuth credential does not grant exactly gmail.modify.")

    def _granted_scopes(self, credentials) -> tuple[str, ...]:
        for attribute in ("granted_scopes", "scopes"):
            value = getattr(credentials, attribute, None)
            parsed = self._normalize_scopes(value)
            if parsed:
                return parsed
        return ()

    def _scopes_from_payload(self, payload: dict[str, Any]) -> tuple[str, ...]:
        for key in ("flowops_granted_scopes", "granted_scopes", "scopes", "scope"):
            parsed = self._normalize_scopes(payload.get(key))
            if parsed:
                return parsed
        return ()

    def _normalize_scopes(self, value: Any) -> tuple[str, ...]:
        if isinstance(value, str):
            return tuple(scope for scope in value.split() if scope)
        if isinstance(value, (list, tuple, set)):
            return tuple(str(scope) for scope in value if str(scope))
        return ()

    def _scopes_are_allowed(self, granted: tuple[str, ...]) -> bool:
        granted_set = set(granted)
        return (
            set(self.scopes).issubset(granted_set)
            and not any(scope in granted_set for scope in GMAIL_UNEXPECTED_BROAD_SCOPES)
        )

    def _keyring(self):
        if self.keyring is not None:
            return self.keyring
        try:
            import keyring
        except ImportError as exc:
            raise GmailOAuthError("keyring is required for secure local token storage.") from exc
        self.keyring = keyring
        return self.keyring

    def _installed_app_flow_class(self):
        try:
            from google_auth_oauthlib.flow import InstalledAppFlow
        except ImportError as exc:
            raise GmailOAuthError("google-auth-oauthlib is required for Gmail OAuth.") from exc
        return InstalledAppFlow

    def _credentials_class(self):
        try:
            from google.oauth2.credentials import Credentials
        except ImportError as exc:
            raise GmailOAuthError("google-auth is required for Gmail OAuth credential loading.") from exc
        return Credentials

    def _google_service_builder(self):
        try:
            from googleapiclient.discovery import build
        except ImportError as exc:
            raise GmailOAuthError("google-api-python-client is required for Gmail service creation.") from exc
        return build
