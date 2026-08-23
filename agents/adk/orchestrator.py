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
from shared.models import AgentEvent, DocumentStatus, JobStatus, LocalStore
from tools.reporting import build_dashboard


ROOT = Path(__file__).resolve().parents[2]
MODEL = "gemini-3.6-flash"


class FlowOpsAdkOrchestrator:
    name = "ADKOrchestrator"

    def __init__(
        self,
        store: LocalStore,
        document_agent: DocumentAgent,
        validation_agent: ValidationAgent,
        decision_agent: DecisionAgent,
        reporting_agent: ReportingAgent,
    ):
        self.store = store
        self.document_agent = document_agent
        self.validation_agent = validation_agent
        self.decision_agent = decision_agent
        self.reporting_agent = reporting_agent

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
                extraction = self.document_agent.process(document)
                validation = self.validation_agent.validate(document, extraction)
                decision = self.decision_agent.decide(document, extraction, validation)
                document = self.store.documents[document.id]
                if decision != "RETRY":
                    break
        return self.reporting_agent.refresh_job_metrics(job_id)

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
