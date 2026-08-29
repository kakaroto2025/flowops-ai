from __future__ import annotations

from pathlib import Path
from typing import Any

from .models import CostGuardDecision, CostGuardResult, FinOpsConfig
from .usage_tracker import UsageTracker


class CostGuard:
    def __init__(self, tracker: UsageTracker, config: FinOpsConfig | None = None):
        self.tracker = tracker
        self.config = config or tracker.config

    def can_process_document(self, document: Any) -> CostGuardResult:
        size = self.file_size_bytes(document.storage_path)
        if size is not None and size > self.config.max_file_size_bytes:
            return CostGuardResult(CostGuardDecision.BLOCK, "file_size_limit_exceeded")

        daily = self.tracker.get_daily_usage()
        if daily["documents"] >= self.config.daily_document_limit:
            return CostGuardResult(CostGuardDecision.BLOCK, "daily_document_limit_exceeded")

        monthly = self.tracker.get_monthly_usage()
        monthly_brl = monthly["estimated_ai_cost_brl"]
        if monthly_brl is not None and monthly_brl >= self.config.monthly_soft_budget_brl:
            return CostGuardResult(CostGuardDecision.SOFT_LIMIT_WARNING, "monthly_soft_budget_reached")

        return CostGuardResult(CostGuardDecision.ALLOW)

    def can_call_gemini(self) -> CostGuardResult:
        daily = self.tracker.get_daily_usage()
        if daily["gemini_calls"] >= self.config.daily_gemini_call_limit:
            return CostGuardResult(CostGuardDecision.BLOCK, "daily_gemini_limit_exceeded")

        monthly = self.tracker.get_monthly_usage()
        monthly_brl = monthly["estimated_ai_cost_brl"]
        if monthly_brl is not None and monthly_brl >= self.config.monthly_soft_budget_brl:
            return CostGuardResult(CostGuardDecision.BLOCK, "monthly_soft_budget_reached")

        return CostGuardResult(CostGuardDecision.ALLOW)

    def file_size_bytes(self, path: str | Path) -> int | None:
        try:
            return Path(path).stat().st_size
        except OSError:
            return None
