"""
Pipeline orchestrator — runs Track B → Matching → Track C → Track D in sequence.

Called from the FastAPI background task. All status updates flow through
jobs.status / jobs.status_detail so the Lovable UI can poll them.
"""

from __future__ import annotations

import traceback
from typing import Any

from db import client as db
from tracks.track_b import run_track_b
from tracks.matching import run_matching
from tracks.track_c import run_track_c
from tracks.track_d import run_track_d


async def run_pipeline(
    job_id: str,
    template_type: str,
    source_file_urls: list[str],
    template_bytes: bytes,
) -> None:
    """
    Full async pipeline: B → Matching → C → D.
    On any unrecoverable error, sets jobs.status = "failed" with a clear detail.
    """
    try:
        # ── Track B ──────────────────────────────────────────────────────────
        bm25_index = await run_track_b(job_id, source_file_urls)

        # ── Matching ─────────────────────────────────────────────────────────
        await run_matching(job_id, template_type, bm25_index)

        # ── Track C ──────────────────────────────────────────────────────────
        _draft_bytes = await run_track_c(job_id, template_type, template_bytes)

        # ── Track D ──────────────────────────────────────────────────────────
        await run_track_d(job_id)

    except Exception as exc:
        tb = traceback.format_exc()
        await db.set_job_status(
            job_id,
            "failed",
            f"{type(exc).__name__}: {exc}\n\n{tb[:800]}",
        )
        raise
