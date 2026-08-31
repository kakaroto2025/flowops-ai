from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Protocol

from shared.models import AuthContext, DEVELOPMENT_TENANT_ID, DEVELOPMENT_USER_ID


class AuthProviderError(RuntimeError):
    pass


class AuthTokenError(AuthProviderError):
    pass


class AuthorizationError(AuthProviderError):
    pass


class MembershipRepositoryError(AuthorizationError):
    pass


@dataclass(frozen=True)
class AuthenticatedIdentity:
    external_uid: str
    email: str | None = None
    claims: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class UserMembership:
    external_uid: str
    tenant_id: str
    user_id: str
    status: str = "ACTIVE"
    roles: tuple[str, ...] = ("member",)

    def to_auth_context(self) -> AuthContext:
        if self.status != "ACTIVE":
            raise AuthorizationError("tenant membership is not active")
        return AuthContext(
            user_id=self.user_id,
            tenant_id=self.tenant_id,
            authenticated=True,
            roles=self.roles,
        )


class AuthProvider(Protocol):
    def verify_token(self, token: str) -> AuthenticatedIdentity: ...


class MembershipRepository(Protocol):
    def get_by_external_uid(self, external_uid: str) -> UserMembership | None: ...


class InMemoryMembershipRepository:
    def __init__(self, memberships: list[UserMembership] | None = None):
        self.memberships = {membership.external_uid: membership for membership in memberships or []}

    @classmethod
    def development(cls) -> "InMemoryMembershipRepository":
        return cls(
            [
                UserMembership(
                    external_uid=DEVELOPMENT_USER_ID,
                    tenant_id=DEVELOPMENT_TENANT_ID,
                    user_id=DEVELOPMENT_USER_ID,
                    roles=("developer",),
                )
            ]
        )

    def get_by_external_uid(self, external_uid: str) -> UserMembership | None:
        return self.memberships.get(external_uid)


class FirestoreMembershipRepository:
    def __init__(
        self,
        project_id: str,
        database: str = "(default)",
        collection: str = "memberships",
        firestore_client: Any | None = None,
    ):
        if not project_id:
            raise AuthProviderError("FirestoreMembershipRepository requires GOOGLE_CLOUD_PROJECT")
        if not database:
            raise AuthProviderError("FirestoreMembershipRepository requires FIRESTORE_DATABASE")
        self.project_id = project_id
        self.database = database
        self.collection = collection or "memberships"
        self.client = firestore_client or self._create_client()

    @classmethod
    def from_env(cls) -> "FirestoreMembershipRepository":
        project_id = (os.environ.get("GOOGLE_CLOUD_PROJECT") or os.environ.get("FIREBASE_PROJECT_ID") or "").strip()
        database = os.environ.get("FIRESTORE_DATABASE", "(default)").strip() or "(default)"
        collection = os.environ.get("FLOWOPS_MEMBERSHIP_COLLECTION", "memberships").strip() or "memberships"
        return cls(project_id=project_id, database=database, collection=collection)

    def get_by_external_uid(self, external_uid: str) -> UserMembership | None:
        if not external_uid:
            return None
        try:
            snapshot = self.client.collection(self.collection).document(external_uid).get()
        except Exception as exc:
            raise MembershipRepositoryError("membership lookup failed") from exc
        if not getattr(snapshot, "exists", False):
            return None
        payload = snapshot.to_dict() or {}
        return _membership_from_payload(external_uid, payload)

    def _create_client(self):
        try:
            from google.cloud import firestore
        except ImportError as exc:
            raise AuthProviderError("Firestore membership requires google-cloud-firestore to be installed") from exc
        return firestore.Client(project=self.project_id, database=self.database)


class DevelopmentAuthProvider:
    def verify_token(self, token: str) -> AuthenticatedIdentity:
        return AuthenticatedIdentity(external_uid=DEVELOPMENT_USER_ID)


class FirebaseAuthProvider:
    def __init__(self, project_id: str):
        if not project_id:
            raise AuthProviderError("FIREBASE_PROJECT_ID is required for AUTH_MODE=firebase")
        self.project_id = project_id
        self.firebase_admin, self.firebase_auth = self._load_firebase()
        self.app = self._get_or_create_app()

    def verify_token(self, token: str) -> AuthenticatedIdentity:
        if not token:
            raise AuthTokenError("missing bearer token")
        try:
            decoded = self.firebase_auth.verify_id_token(token, app=self.app, check_revoked=True)
        except Exception as exc:
            raise AuthTokenError("invalid or expired bearer token") from exc

        external_uid = decoded.get("uid") or decoded.get("sub")
        if not external_uid:
            raise AuthTokenError("token missing uid")
        return AuthenticatedIdentity(
            external_uid=str(external_uid),
            email=decoded.get("email"),
            claims=_safe_claims(decoded),
        )

    def _load_firebase(self):
        try:
            import firebase_admin
            from firebase_admin import auth as firebase_auth
        except ImportError as exc:
            raise AuthProviderError("AUTH_MODE=firebase requires firebase-admin to be installed") from exc
        return firebase_admin, firebase_auth

    def _get_or_create_app(self):
        name = f"flowops-{self.project_id}"
        try:
            return self.firebase_admin.get_app(name)
        except ValueError:
            return self.firebase_admin.initialize_app(options={"projectId": self.project_id}, name=name)


def auth_mode(value: str | None = None) -> str:
    mode = (value or os.environ.get("AUTH_MODE") or "development").strip().lower()
    if mode not in {"development", "firebase"}:
        raise AuthProviderError("Unsupported AUTH_MODE")
    return mode


def _safe_claims(decoded: dict[str, Any]) -> dict[str, Any]:
    allowed = {"iss", "aud", "email_verified", "auth_time"}
    return {key: decoded[key] for key in allowed if key in decoded}


def _membership_from_payload(expected_uid: str, payload: dict[str, Any]) -> UserMembership:
    external_uid = str(payload.get("external_uid") or expected_uid)
    if external_uid != expected_uid:
        raise MembershipRepositoryError("membership external_uid mismatch")
    tenant_id = str(payload.get("tenant_id") or "")
    user_id = str(payload.get("user_id") or external_uid)
    status = str(payload.get("status") or "").upper()
    roles = payload.get("roles", ["member"])
    if not isinstance(roles, (list, tuple)) or not roles:
        raise MembershipRepositoryError("membership roles are invalid")
    try:
        membership = UserMembership(
            external_uid=external_uid,
            tenant_id=tenant_id,
            user_id=user_id,
            status=status,
            roles=tuple(str(role) for role in roles),
        )
        if membership.status == "ACTIVE":
            membership.to_auth_context()
        return membership
    except ValueError as exc:
        raise MembershipRepositoryError("membership payload is invalid") from exc
