"""
FastAPI application — exposes exactly two HTTP endpoints:
  POST /pipeline/start   → accepts job, fires async pipeline
  POST /pipeline/export  → requires all reviews done, returns final .docx URL

All other logic is internal.
"""

from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from typing import Any

from fastapi import BackgroundTasks, FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from pydantic import BaseModel

from config import settings
from db import client as db
from pipeline import run_pipeline


# ---------------------------------------------------------------------------
# App setup
# ---------------------------------------------------------------------------

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Validate config on startup
    missing = []
    if not settings.insforge_service_key:
        missing.append("INSFORGE_SERVICE_KEY")
    if not settings.anthropic_api_key:
        missing.append("ANTHROPIC_API_KEY")
    if missing:
        print(f"WARNING: Missing environment variables: {', '.join(missing)}")
    yield


app = FastAPI(
    title="Regulatory Document Pipeline",
    version="1.0.0",
    lifespan=lifespan,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["POST", "GET"],
    allow_headers=["*"],
)


# ---------------------------------------------------------------------------
# Request / response models
# ---------------------------------------------------------------------------

class StartRequest(BaseModel):
    job_id: str
    template_type: str
    source_file_urls: list[str]
    # Optional: URL of the blank template in Insforge Storage.
    # If omitted, we look at templates/{template_type}/blank.docx in the default bucket.
    template_file_url: str | None = None


class StartResponse(BaseModel):
    accepted: bool


class ExportRequest(BaseModel):
    job_id: str


class ExportResponse(BaseModel):
    file_url: str


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@app.post("/pipeline/start", response_model=StartResponse)
async def pipeline_start(
    body: StartRequest,
    background_tasks: BackgroundTasks,
) -> StartResponse:
    """
    Validate inputs, update jobs row, start async pipeline.
    Returns immediately; progress visible via jobs.status polling.
    """
    # Validate template_type
    allowed_types = {"2.6.7_tox_summary", "csr"}
    if body.template_type not in allowed_types:
        raise HTTPException(
            status_code=400,
            detail=f"Unknown template_type '{body.template_type}'. Allowed: {sorted(allowed_types)}",
        )

    if not body.source_file_urls:
        raise HTTPException(status_code=400, detail="source_file_urls must not be empty")

    # Ensure job row exists (Lovable creates it, but we upsert to be safe)
    existing_job = await db.get_job(body.job_id)
    if existing_job is None:
        await db.insert("jobs", {
            "job_id": body.job_id,
            "template_type": body.template_type,
            "status": "uploading",
            "status_detail": "Job received by pipeline service",
        })
    else:
        # Allow retry: reset status
        await db.set_job_status(body.job_id, "uploading", "Pipeline restarted")

    # Resolve template file URL
    encoded_key = f"templates%2F{body.template_type}%2Fblank.docx"
    template_url = body.template_file_url or (
        f"{settings.insforge_api_url.rstrip('/')}/api/storage/buckets/"
        f"{settings.insforge_storage_bucket}/objects/{encoded_key}"
    )

    # Download template now (before backgrounding — gives immediate error if missing)
    try:
        template_bytes = await db.download_storage_file(template_url)
    except Exception as exc:
        await db.set_job_status(body.job_id, "failed", f"Could not download template: {exc}")
        raise HTTPException(status_code=400, detail=f"Template download failed: {exc}")

    # Fire-and-forget pipeline
    background_tasks.add_task(
        run_pipeline,
        body.job_id,
        body.template_type,
        body.source_file_urls,
        template_bytes,
    )

    return StartResponse(accepted=True)


@app.post("/pipeline/export", response_model=ExportResponse)
async def pipeline_export(body: ExportRequest) -> ExportResponse:
    """
    Apply reviewer decisions and return the final .docx download URL.
    Requires every verification_results row to have reviewer_action != "pending".
    """
    job = await db.get_job(body.job_id)
    if not job:
        raise HTTPException(status_code=404, detail=f"Job {body.job_id} not found")

    if job["status"] != "done":
        raise HTTPException(
            status_code=400,
            detail=f"Job is not complete (status={job['status']}). Wait for status='done' before exporting.",
        )

    # Check all reviews are resolved
    write_log = await db.get_write_log(body.job_id)
    if not write_log:
        raise HTTPException(status_code=400, detail="No write_log entries found for this job.")

    write_ids = {row["write_id"] for row in write_log}

    # Fetch verification_results for this job
    ver_rows = await db.select(
        "verification_results",
        columns="write_id,verdict,reviewer_action,reviewer_edit_value",
    )
    # Filter to this job's write_ids
    job_ver = [r for r in ver_rows if r["write_id"] in write_ids]
    pending = [r for r in job_ver if r.get("reviewer_action", "pending") == "pending"]
    if pending:
        raise HTTPException(
            status_code=400,
            detail=f"{len(pending)} verification result(s) still pending reviewer action. "
            "All entries must be accepted, edited, or rejected before export.",
        )

    # Build reviewer decisions map: write_id → {action, edit_value}
    decisions: dict[str, dict[str, Any]] = {
        r["write_id"]: {
            "action": r.get("reviewer_action", "accepted"),
            "edit_value": r.get("reviewer_edit_value"),
        }
        for r in job_ver
    }

    # Download the pipeline-generated draft
    encoded_draft_key = f"drafts%2F{body.job_id}%2Fdraft.docx"
    draft_url = (
        f"{settings.insforge_api_url.rstrip('/')}/api/storage/buckets/"
        f"{settings.insforge_storage_bucket}/objects/{encoded_draft_key}"
    )
    try:
        draft_bytes = await db.download_storage_file(draft_url)
    except Exception as exc:
        raise HTTPException(status_code=500, detail=f"Could not download draft: {exc}")

    # Apply reviewer overrides
    final_bytes = await _apply_reviewer_decisions(draft_bytes, write_log, decisions)

    # Upload final document
    final_path = f"finals/{body.job_id}/final.docx"
    final_url = await db.upload_storage_file(
        settings.insforge_storage_bucket,
        final_path,
        final_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    return ExportResponse(file_url=final_url)


async def _apply_reviewer_decisions(
    draft_bytes: bytes,
    write_log: list[dict[str, Any]],
    decisions: dict[str, dict[str, Any]],
) -> bytes:
    """
    Apply reviewer_action overrides to the draft document:
      - "accepted": leave cell as generated.
      - "edited":   replace cell with reviewer_edit_value.
      - "rejected": blank out the cell.
    """
    from parsers.docx_parser import DocxCellWriter

    writer = DocxCellWriter(draft_bytes)

    for row in write_log:
        write_id = row["write_id"]
        location = row["template_cell_location"]
        decision = decisions.get(write_id, {"action": "accepted", "edit_value": None})
        action = decision["action"]

        if action == "edited":
            new_val = decision.get("edit_value") or ""
            writer.write_cell(location, new_val)
        elif action == "rejected":
            writer.write_cell(location, "")
        # "accepted" → no change needed (draft already has the generated value)

    return writer.get_bytes()


# ---------------------------------------------------------------------------
# Health check
# ---------------------------------------------------------------------------

@app.get("/health")
async def health() -> dict:
    return {"status": "ok", "service": "regulatory-doc-pipeline"}


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host=settings.host, port=settings.port, reload=False)
