from __future__ import annotations

import os
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from enum import StrEnum
from typing import Any


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return default
    return int(raw)


def env_float(name: str) -> float | None:
    raw = os.environ.get(name)
    if raw in (None, ""):
        return None
    return float(raw)


class CostGuardDecision(StrEnum):
    ALLOW = "ALLOW"
    SOFT_LIMIT_WARNING = "SOFT_LIMIT_WARNING"
    BLOCK = "BLOCK"


@dataclass
class CostGuardResult:
    decision: str
    reason: str | None = None

    @property
    def allowed(self) -> bool:
        return self.decision != CostGuardDecision.BLOCK

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass
class FinOpsConfig:
    free_tier_first: bool = True
    daily_document_limit: int = 50
    daily_gemini_call_limit: int = 100
    max_file_size_mb: int = 10
    monthly_soft_budget_brl: float = 50.0
    usd_brl_rate: float | None = None

    @classmethod
    def from_env(cls) -> "FinOpsConfig":
        return cls(
            free_tier_first=env_bool("FREE_TIER_FIRST", True),
            daily_document_limit=env_int("FLOWOPS_DAILY_DOCUMENT_LIMIT", 50),
            daily_gemini_call_limit=env_int("FLOWOPS_DAILY_GEMINI_CALL_LIMIT", 100),
            max_file_size_mb=env_int("FLOWOPS_MAX_FILE_SIZE_MB", 10),
            monthly_soft_budget_brl=env_float("FLOWOPS_MONTHLY_SOFT_BUDGET_BRL") or 50.0,
            usd_brl_rate=env_float("FLOWOPS_USD_BRL_RATE"),
        )

    @property
    def max_file_size_bytes(self) -> int:
        return self.max_file_size_mb * 1024 * 1024


@dataclass
class UsageRecord:
    id: str
    job_id: str
    document_id: str
    timestamp: str = field(default_factory=utc_now)
    tenant_id: str = "tenant_default"
    document_type: str | None = None
    country: str | None = None
    file_size_bytes: int | None = None
    gemini_used: bool = False
    gemini_model: str | None = None
    gemini_calls: int = 0
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_ai_cost_usd: float | None = None
    estimated_ai_cost_brl: float | None = None
    parser_fallback_used: bool = False
    processing_status: str | None = None
    blocked_by_cost_guard: bool = False
    block_reason: str | None = None
    warning_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
