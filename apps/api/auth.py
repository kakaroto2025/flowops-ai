from __future__ import annotations

import os
from typing import Annotated

from fastapi import Depends, HTTPException, Request

from shared.models import AuthContext, development_auth_context
from tools.auth import (
    AuthProvider,
    AuthProviderError,
    AuthTokenError,
    AuthorizationError,
    DevelopmentAuthProvider,
    FirebaseAuthProvider,
    InMemoryMembershipRepository,
    MembershipRepository,
    auth_mode,
)


def get_auth_context(request: Request) -> AuthContext:
    mode = auth_mode()
    if mode == "development":
        return development_auth_context()

    token = _bearer_token(request)
    provider = get_auth_provider()
    membership_repository = get_membership_repository()
    try:
        identity = provider.verify_token(token)
    except AuthTokenError as exc:
        raise HTTPException(status_code=401, detail="invalid_or_expired_token") from exc
    except AuthProviderError as exc:
        raise HTTPException(status_code=401, detail="authentication_unavailable") from exc

    membership = membership_repository.get_by_external_uid(identity.external_uid)
    if membership is None:
        raise HTTPException(status_code=403, detail="tenant_membership_required")
    try:
        return membership.to_auth_context()
    except AuthorizationError as exc:
        raise HTTPException(status_code=403, detail="tenant_membership_inactive") from exc


AuthDependency = Annotated[AuthContext, Depends(get_auth_context)]


def get_auth_provider() -> AuthProvider:
    mode = auth_mode()
    if mode == "development":
        return DevelopmentAuthProvider()
    return FirebaseAuthProvider(project_id=os.environ.get("FIREBASE_PROJECT_ID", "").strip())


def get_membership_repository() -> MembershipRepository:
    if auth_mode() == "development":
        return InMemoryMembershipRepository.development()
    return InMemoryMembershipRepository(_firebase_test_memberships_from_env())


def _bearer_token(request: Request) -> str:
    authorization = request.headers.get("authorization", "")
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer" or not token.strip():
        raise HTTPException(status_code=401, detail="bearer_token_required")
    return token.strip()


def _firebase_test_memberships_from_env():
    raw = os.environ.get("FLOWOPS_TEST_MEMBERSHIPS", "").strip()
    if not raw:
        return []
    from tools.auth import UserMembership

    memberships = []
    for item in raw.split(","):
        parts = [part.strip() for part in item.split(":")]
        if len(parts) < 3:
            continue
        external_uid, tenant_id, user_id = parts[:3]
        status = parts[3] if len(parts) > 3 else "ACTIVE"
        memberships.append(UserMembership(external_uid=external_uid, tenant_id=tenant_id, user_id=user_id, status=status))
    return memberships
