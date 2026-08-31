from __future__ import annotations

from dataclasses import fields
from datetime import datetime, timezone
from typing import Any

from .models import FinOpsConfig, UsageRecord


class UsageTracker:
    def __init__(self, store: Any, config: FinOpsConfig | None = None, tenant_id: str | None = None):
        self.store = store
        self.config = config or FinOpsConfig.from_env()
        self.tenant_id = tenant_id

    def record_usage(self, record: UsageRecord) -> UsageRecord:
        if not record.id:
            record.id = self.store.next_id("usage")
        self.store.add_finops_usage_record(record)
        return record

    def get_daily_usage(self) -> dict[str, Any]:
        return self._aggregate(self._records_in_current_day())

    def get_monthly_usage(self) -> dict[str, Any]:
        return self._aggregate(self._records_in_current_month())

    def get_usage_summary(self) -> dict[str, Any]:
        daily = self.get_daily_usage()
        monthly = self.get_monthly_usage()
        budget = self.config.monthly_soft_budget_brl
        monthly_brl = monthly["estimated_ai_cost_brl"]
        percentage = None
        if monthly_brl is not None and budget:
            percentage = round(monthly_brl / budget * 100, 4)
        return {
            "free_tier_first": self.config.free_tier_first,
            "documents_today": daily["documents"],
            "documents_month": monthly["documents"],
            "gemini_calls_today": daily["gemini_calls"],
            "gemini_calls_month": monthly["gemini_calls"],
            "input_tokens_today": daily["input_tokens"],
            "output_tokens_today": daily["output_tokens"],
            "input_tokens_month": monthly["input_tokens"],
            "output_tokens_month": monthly["output_tokens"],
            "estimated_ai_cost_today_usd": daily["estimated_ai_cost_usd"],
            "estimated_ai_cost_month_usd": monthly["estimated_ai_cost_usd"],
            "estimated_ai_cost_today_brl": daily["estimated_ai_cost_brl"],
            "estimated_ai_cost_month_brl": monthly["estimated_ai_cost_brl"],
            "average_cost_per_document": self._average_cost_per_document(monthly),
            "monthly_soft_budget_brl": budget,
            "percentage_of_internal_budget_used": percentage,
            "daily_document_limit": self.config.daily_document_limit,
            "daily_gemini_call_limit": self.config.daily_gemini_call_limit,
            "max_file_size_mb": self.config.max_file_size_mb,
        }

    def reset(self) -> None:
        self.store.finops_usage_records.clear()
        self.store.save()

    def _records_in_current_day(self) -> list[UsageRecord]:
        now = datetime.now(timezone.utc)
        return [record for record in self._records() if _parse_dt(record.timestamp).date() == now.date()]

    def _records_in_current_month(self) -> list[UsageRecord]:
        now = datetime.now(timezone.utc)
        return [
            record
            for record in self._records()
            if _parse_dt(record.timestamp).year == now.year and _parse_dt(record.timestamp).month == now.month
        ]

    def _records(self) -> list[UsageRecord]:
        values = getattr(self.store, "finops_usage_records", {}).values()
        records = [record if isinstance(record, UsageRecord) else _usage_from_dict(record) for record in values]
        if self.tenant_id is None:
            return records
        return [record for record in records if record.tenant_id == self.tenant_id]

    def _aggregate(self, records: list[UsageRecord]) -> dict[str, Any]:
        return {
            "documents": len(records),
            "gemini_calls": sum(record.gemini_calls or 0 for record in records),
            "input_tokens": _sum_optional(record.input_tokens for record in records),
            "output_tokens": _sum_optional(record.output_tokens for record in records),
            "estimated_ai_cost_usd": _sum_optional(record.estimated_ai_cost_usd for record in records),
            "estimated_ai_cost_brl": _sum_optional(record.estimated_ai_cost_brl for record in records),
        }

    def _average_cost_per_document(self, monthly: dict[str, Any]) -> float | None:
        if not monthly["documents"] or monthly["estimated_ai_cost_usd"] is None:
            return None
        return round(monthly["estimated_ai_cost_usd"] / monthly["documents"], 8)


def _sum_optional(values) -> int | float | None:
    items = [value for value in values if value is not None]
    if not items:
        return None
    return round(sum(items), 8) if any(isinstance(item, float) for item in items) else sum(items)


def _parse_dt(value: str) -> datetime:
    return datetime.fromisoformat(value.replace("Z", "+00:00"))


def _usage_from_dict(payload: dict[str, Any]) -> UsageRecord:
    allowed = {field.name for field in fields(UsageRecord)}
    return UsageRecord(**{key: value for key, value in payload.items() if key in allowed})
