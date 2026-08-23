from __future__ import annotations

from typing import Any

from shared.models import AgentEvent, LocalStore


class BaseAgent:
    name = "BaseAgent"

    def __init__(self, store: LocalStore):
        self.store = store

    def event(
        self,
        job_id: str,
        event_type: str,
        message: str,
        document_id: str | None = None,
        data: dict[str, Any] | None = None,
    ) -> AgentEvent:
        event = AgentEvent(
            id=self.store.next_id("evt"),
            job_id=job_id,
            document_id=document_id,
            agent=self.name,
            event_type=event_type,
            message=message,
            data=data or {},
        )
        return self.store.add_event(event)

