from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any


DEVELOPMENT_TENANT_ID = "tenant_default"
DEVELOPMENT_USER_ID = "local_dev_user"


class TenantContextError(ValueError):
    pass


@dataclass(frozen=True)
class TenantContext:
    tenant_id: str
    name: str
    status: str = "ACTIVE"

    def __post_init__(self) -> None:
        if not _is_safe_id(self.tenant_id):
            raise TenantContextError("tenant_id must be a safe stable identifier")

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class AuthContext:
    user_id: str
    tenant_id: str
    authenticated: bool
    roles: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not _is_safe_id(self.tenant_id):
            raise TenantContextError("tenant_id must be a safe stable identifier")
        if not _is_safe_id(self.user_id):
            raise TenantContextError("user_id must be a safe stable identifier")

    def require_tenant_id(self) -> str:
        if not self.authenticated or not self.tenant_id:
            raise TenantContextError("authenticated tenant context is required")
        return self.tenant_id

    def to_dict(self) -> dict[str, Any]:
        return {**asdict(self), "roles": list(self.roles)}


def development_auth_context() -> AuthContext:
    return AuthContext(
        user_id=DEVELOPMENT_USER_ID,
        tenant_id=DEVELOPMENT_TENANT_ID,
        authenticated=True,
        roles=("developer",),
    )


def require_tenant_id(auth_context: AuthContext | None) -> str:
    if auth_context is None:
        raise TenantContextError("tenant context is required")
    return auth_context.require_tenant_id()


def _is_safe_id(value: str) -> bool:
    return bool(re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{2,63}", value or ""))
