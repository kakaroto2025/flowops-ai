from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel
from starlette.concurrency import run_in_threadpool

from apps.api.auth import AuthDependency
from apps.api.processor import JobProcessor
from shared.models import create_persistence_store
from tools.reporting import (
    build_dashboard,
    build_global_erp_records,
    build_global_human_review_queue,
    build_job_history,
    build_report,
)


app = FastAPI(title="FlowOps AI API", version="0.1.0")
store = create_persistence_store()
processor = JobProcessor(store)
WEB_DIR = Path("apps/web")
APP_ENV = os.environ.get("APP_ENV", "local").strip().lower()

if WEB_DIR.exists():
    app.mount("/assets", StaticFiles(directory=WEB_DIR), name="assets")


class ResolveReviewRequest(BaseModel):
    corrected_fields: dict[str, Any]
    reviewer: str = "demo_user"


class RejectReviewRequest(BaseModel):
    reviewer: str = "demo_user"


@app.get("/")
def web_dashboard() -> FileResponse:
    index_path = WEB_DIR / "index.html"
    if not index_path.exists():
        raise HTTPException(status_code=404, detail="web_dashboard_not_found")
    return FileResponse(index_path, headers={"Cache-Control": "no-store"})


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok", "service": "flowops-ai-api"}


if APP_ENV != "production":
    @app.post("/dev/reset")
    def reset_state() -> dict[str, str]:
        store.reset()
        return {"status": "reset"}


@app.post("/jobs/demo")
def create_demo_job(auth_context: AuthDependency, processing_region: str = "AUTO") -> dict[str, Any]:
    job = _processor_for(auth_context).create_demo_job(processing_region=processing_region)
    return job.to_dict()


@app.post("/jobs/demo/run")
def create_and_run_demo_job(auth_context: AuthDependency, processing_region: str = "AUTO") -> dict[str, Any]:
    request_processor = _processor_for(auth_context)
    job = request_processor.create_demo_job(processing_region=processing_region)
    return request_processor.run_job(job.id)


@app.post("/jobs/upload/run")
async def upload_and_run_job(
    auth_context: AuthDependency,
    files: list[UploadFile] = File(...),
    processing_region: str = Form("AUTO"),
) -> dict[str, Any]:
    if not files:
        raise HTTPException(status_code=400, detail="no_files_uploaded")

    upload_root = Path("local_data/uploads")
    upload_root.mkdir(parents=True, exist_ok=True)
    batch_id = store.next_id("upload")
    batch_dir = upload_root / batch_id
    batch_dir.mkdir(parents=True, exist_ok=True)

    saved_files: list[Path] = []
    for upload in files:
        original_name = Path(upload.filename or "document.pdf").name
        if not original_name.lower().endswith(".pdf"):
            raise HTTPException(status_code=400, detail=f"unsupported_file_type:{original_name}")
        destination = batch_dir / original_name
        destination.write_bytes(await upload.read())
        saved_files.append(destination)

    request_processor = _processor_for(auth_context)
    job = request_processor.create_upload_job(saved_files, processing_region=normalize_region(processing_region))
    return await run_in_threadpool(request_processor.run_job, job.id)


@app.get("/jobs")
def list_jobs(auth_context: AuthDependency) -> list[dict[str, Any]]:
    return [row for row in build_job_history(store) if store.jobs[row["job_id"]].tenant_id == auth_context.tenant_id]


@app.get("/human-reviews")
def list_human_reviews(auth_context: AuthDependency) -> list[dict[str, Any]]:
    return [row for row in build_global_human_review_queue(store) if row.get("tenant_id") == auth_context.tenant_id]


@app.get("/erp-records")
def list_erp_records(auth_context: AuthDependency) -> list[dict[str, Any]]:
    return [row for row in build_global_erp_records(store) if row.get("tenant_id") == auth_context.tenant_id]


@app.get("/api/finops/usage")
def finops_usage(auth_context: AuthDependency) -> dict[str, Any]:
    return _processor_for(auth_context).usage_tracker.get_usage_summary()


@app.get("/jobs/{job_id}")
def get_job(job_id: str, auth_context: AuthDependency) -> dict[str, Any]:
    if job_id not in store.jobs:
        raise HTTPException(status_code=404, detail="job_not_found")
    _ensure_job_access(auth_context, job_id)
    return build_dashboard(store, job_id)


@app.post("/jobs/{job_id}/run")
def run_job(job_id: str, auth_context: AuthDependency) -> dict[str, Any]:
    if job_id not in store.jobs:
        raise HTTPException(status_code=404, detail="job_not_found")
    _ensure_job_access(auth_context, job_id)
    return _processor_for(auth_context).run_job(job_id)


@app.get("/jobs/{job_id}/events")
def get_events(job_id: str, auth_context: AuthDependency) -> list[dict[str, Any]]:
    if job_id not in store.jobs:
        raise HTTPException(status_code=404, detail="job_not_found")
    _ensure_job_access(auth_context, job_id)
    return [event.to_dict() for event in store.events_for_job(job_id)]


@app.get("/jobs/{job_id}/dashboard")
def get_dashboard(job_id: str, auth_context: AuthDependency) -> dict[str, Any]:
    if job_id not in store.jobs:
        raise HTTPException(status_code=404, detail="job_not_found")
    _ensure_job_access(auth_context, job_id)
    return build_dashboard(store, job_id)


@app.get("/jobs/{job_id}/report")
def get_report(job_id: str, auth_context: AuthDependency) -> dict[str, Any]:
    if job_id not in store.jobs:
        raise HTTPException(status_code=404, detail="job_not_found")
    _ensure_job_access(auth_context, job_id)
    return build_report(store, job_id)


@app.post("/human-reviews/{review_id}/resolve")
def resolve_review(review_id: str, payload: ResolveReviewRequest, auth_context: AuthDependency) -> dict[str, Any]:
    if review_id not in store.human_reviews:
        raise HTTPException(status_code=404, detail="review_not_found")
    _ensure_review_access(auth_context, review_id)
    try:
        return _processor_for(auth_context).resolve_review(
            review_id,
            corrected_fields=payload.corrected_fields,
            reviewer=payload.reviewer,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.post("/human-reviews/{review_id}/reject")
def reject_review(review_id: str, auth_context: AuthDependency, payload: RejectReviewRequest | None = None) -> dict[str, Any]:
    if review_id not in store.human_reviews:
        raise HTTPException(status_code=404, detail="review_not_found")
    _ensure_review_access(auth_context, review_id)
    try:
        return _processor_for(auth_context).reject_review(review_id, reviewer=payload.reviewer if payload else "demo_user")
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/demo/sample-files")
def sample_files() -> list[str]:
    sample_dir = Path("sample_data/alfa_contabilidade")
    return [path.name for path in sorted(sample_dir.glob("*.pdf"))]
from tools.documents.normalization import normalize_region


def _processor_for(auth_context) -> JobProcessor:
    return JobProcessor(store, auth_context=auth_context)


def _ensure_job_access(auth_context, job_id: str) -> None:
    try:
        store.job_for_tenant(auth_context.tenant_id, job_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="job_not_found") from exc
    except Exception as exc:
        raise HTTPException(status_code=403, detail="tenant_access_denied") from exc


def _ensure_review_access(auth_context, review_id: str) -> None:
    try:
        store.review_for_tenant(auth_context.tenant_id, review_id)
    except KeyError as exc:
        raise HTTPException(status_code=404, detail="review_not_found") from exc
    except Exception as exc:
        raise HTTPException(status_code=403, detail="tenant_access_denied") from exc
