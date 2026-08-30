from __future__ import annotations

import asyncio
import contextlib
import io
import os
import traceback
from pathlib import Path
from typing import Any

from google.adk.agents import Agent
from google.adk.runners import Runner
from google.adk.sessions import InMemorySessionService
from google.genai import types

from agents.decision import DecisionAgent
from agents.document import DocumentAgent
from agents.reporting import ReportingAgent
from agents.validation import ValidationAgent
from shared.models import AgentEvent, DocumentStatus, JobStatus, PersistenceStore
from tools.finops import CostGuard, UsageRecord, UsageTracker
from tools.reporting import build_dashboard


ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-3.6-flash"


class FlowOpsAdkOrchestrator:
    name = "ADKOrchestrator"

    def __init__(
        self,
        store: PersistenceStore,
        document_agent: DocumentAgent,
        validation_agent: ValidationAgent,
        decision_agent: DecisionAgent,
        reporting_agent: ReportingAgent,
        cost_guard: CostGuard | None = None,
        usage_tracker: UsageTracker | None = None,
    ):
        self.store = store
        self.document_agent = document_agent
        self.validation_agent = validation_agent
        self.decision_agent = decision_agent
        self.reporting_agent = reporting_agent
        self.cost_guard = cost_guard
        self.usage_tracker = usage_tracker

    def run_job(self, job_id: str) -> dict[str, Any]:
        self._event(job_id, "ADK_WORKFLOW_STARTED", "ADK workflow started.")
        self.store.update_job(job_id, status=JobStatus.PROCESSING)

        try:
            try:
                adk_result = self._confirm_adk_runtime(job_id)
                self._event(
                    job_id,
                    "ADK_ORCHESTRATOR_READY",
                    "ADK orchestrator acknowledged the workflow.",
                    data={"model": MODEL, "result": adk_result},
                )
            except Exception as exc:
                if not self._is_recoverable_adk_model_error(exc):
                    raise
                self._event(
                    job_id,
                    "ADK_ORCHESTRATOR_UNAVAILABLE",
                    "ADK model acknowledgement unavailable; continuing workflow safely.",
                    data=self._safe_error_details(exc),
                )
            dashboard = self._run_existing_agents(job_id)
            self._event(
                job_id,
                "ADK_WORKFLOW_COMPLETED",
                "ADK workflow completed.",
                data={
                    "documents_processed": dashboard["kpis"]["documents_processed"],
                    "erp_records": dashboard["kpis"]["erp_records"],
                    "human_reviews": dashboard["kpis"]["human_reviews"],
                },
            )
            return self.reporting_agent.refresh_job_metrics(job_id)
        except Exception as exc:
            self._event(
                job_id,
                "ADK_WORKFLOW_FAILED",
                "ADK workflow failed safely.",
                data=self._safe_error_details(exc),
            )
            self.store.update_job(job_id, status=JobStatus.FAILED, failed_count=len(self.store.documents_for_job(job_id)))
            return build_dashboard(self.store, job_id)

    def _run_existing_agents(self, job_id: str) -> dict[str, Any]:
        documents = self.store.documents_for_job(job_id)
        for document in documents:
            while document.status in {DocumentStatus.QUEUED, DocumentStatus.RETRY}:
                guard_result = self.cost_guard.can_process_document(document) if self.cost_guard else None
                if guard_result and not guard_result.allowed:
                    self.store.update_document(document.id, status=DocumentStatus.FAILED)
                    self._event(
                        job_id,
                        "FINOPS_BLOCK",
                        "Cost Guard blocked document before processing.",
                        document.id,
                        guard_result.to_dict(),
                    )
                    self._record_finops_usage(
                        document.id,
                        {
                            "file_size_bytes": self.cost_guard.file_size_bytes(document.storage_path),
                            "blocked_by_cost_guard": True,
                            "block_reason": guard_result.reason,
                            "processing_status": DocumentStatus.FAILED,
                        },
                    )
                    break
                if guard_result and guard_result.reason:
                    self._event(
                        job_id,
                        "FINOPS_WARNING",
                        "Cost Guard soft warning before document processing.",
                        document.id,
                        guard_result.to_dict(),
                    )
                elif guard_result:
                    self._event(
                        job_id,
                        "FINOPS_ALLOW",
                        "Cost Guard allowed document processing.",
                        document.id,
                        guard_result.to_dict(),
                    )
                extraction = self.document_agent.process(document)
                validation = self.validation_agent.validate(document, extraction)
                decision = self.decision_agent.decide(document, extraction, validation)
                document = self.store.documents[document.id]
                self._record_finops_usage(document.id, {"processing_status": document.status})
                if decision != "RETRY":
                    break
        return self.reporting_agent.refresh_job_metrics(job_id)

    def _record_finops_usage(self, document_id: str, overrides: dict[str, Any] | None = None) -> None:
        if not self.usage_tracker:
            return
        document = self.store.documents[document_id]
        payload = dict(getattr(self.document_agent, "last_finops_usage", {}).pop(document_id, {}) or {})
        payload.update(overrides or {})
        record = UsageRecord(
            id=self.store.next_id("usage"),
            job_id=document.job_id,
            document_id=document.id,
            document_type=payload.get("document_type"),
            country=payload.get("country"),
            file_size_bytes=payload.get("file_size_bytes"),
            gemini_used=bool(payload.get("gemini_used", False)),
            gemini_model=payload.get("gemini_model"),
            gemini_calls=int(payload.get("gemini_calls") or 0),
            input_tokens=payload.get("input_tokens"),
            output_tokens=payload.get("output_tokens"),
            total_tokens=payload.get("total_tokens"),
            estimated_ai_cost_usd=payload.get("estimated_ai_cost_usd"),
            estimated_ai_cost_brl=payload.get("estimated_ai_cost_brl"),
            parser_fallback_used=bool(payload.get("parser_fallback_used", False)),
            processing_status=str(payload.get("processing_status") or document.status),
            blocked_by_cost_guard=bool(payload.get("blocked_by_cost_guard", False)),
            block_reason=payload.get("block_reason"),
            warning_reason=payload.get("warning_reason"),
        )
        self.usage_tracker.record_usage(record)
        self._event(
            document.job_id,
            "FINOPS_USAGE_RECORDED",
            "FinOps usage record stored.",
            document.id,
            {
                "usage_record_id": record.id,
                "gemini_used": record.gemini_used,
                "gemini_calls": record.gemini_calls,
                "blocked_by_cost_guard": record.blocked_by_cost_guard,
            },
        )

    def _confirm_adk_runtime(self, job_id: str) -> str:
        return asyncio.run(self._confirm_adk_runtime_async(job_id))

    async def _confirm_adk_runtime_async(self, job_id: str) -> str:
        self._configure_google_api_key()
        agent = Agent(
            name="flowops_pipeline_orchestrator",
            model=MODEL,
            instruction="Voce e o orquestrador do FlowOps AI. Responda exatamente READY.",
        )
        session_service = InMemorySessionService()
        app_name = "flowops_ai"
        user_id = "flowops_system"
        session_id = f"{job_id}_adk"
        await session_service.create_session(
            app_name=app_name,
            user_id=user_id,
            session_id=session_id,
        )
        runner = Runner(
            app_name=app_name,
            agent=agent,
            session_service=session_service,
        )
        message = types.Content(
            role="user",
            parts=[types.Part(text="Confirme o inicio do workflow. Responda somente READY.")],
        )

        texts: list[str] = []
        with contextlib.redirect_stderr(io.StringIO()):
            async for event in runner.run_async(user_id=user_id, session_id=session_id, new_message=message):
                content = getattr(event, "content", None)
                if not content or not getattr(content, "parts", None):
                    continue
                for part in content.parts:
                    text = getattr(part, "text", None)
                    if text:
                        texts.append(text.strip())
        result = "\n".join(texts).strip()
        if result != "READY":
            raise RuntimeError("adk_orchestrator_unexpected_response")
        return result

    def _configure_google_api_key(self) -> None:
        env_path = ROOT / ".env"
        if env_path.exists():
            for raw_line in env_path.read_text(encoding="utf-8").splitlines():
                line = raw_line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))

        api_key = os.environ.get("GEMINI_API_KEY", "").strip()
        if not api_key or api_key == "SUA_CHAVE_REAL_AQUI":
            raise RuntimeError("gemini_api_key_not_configured_for_adk")
        os.environ["GOOGLE_API_KEY"] = api_key

    def _event(
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

    def _safe_error_details(self, exc: Exception) -> dict[str, Any]:
        frames = traceback.extract_tb(exc.__traceback__)
        last_frame = frames[-1] if frames else None
        return {
            "error_type": type(exc).__name__,
            "error_message": str(exc)[:500],
            "file": Path(last_frame.filename).name if last_frame else None,
            "function": last_frame.name if last_frame else None,
            "line": last_frame.lineno if last_frame else None,
            "stack_trace": [
                {
                    "file": Path(frame.filename).name,
                    "function": frame.name,
                    "line": frame.lineno,
                }
                for frame in frames[-6:]
            ],
        }

    def _is_recoverable_adk_model_error(self, exc: Exception) -> bool:
        text = str(exc)
        error_type = type(exc).__name__
        return (
            "ResourceExhausted" in error_type
            or "RESOURCE_EXHAUSTED" in text
            or "429" in text
            or "quota" in text.lower()
        )
