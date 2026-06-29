"""
Track B — Source Document Processing (per job).

Steps B1-B5 (C2 removed per spec):
  B1. Receive source file URLs for the job (files already in Insforge Storage).
  B2. File-type routing: detect PDF vs DOCX, route to correct parser.
  B3. Raw extraction — no LLM; write every unit to raw_spans.
  B4. Build BM25 index over the job's spans (returned for use in Matching).
  B5. Sanity check: fail fast if extraction looks empty or broken.

Status transitions:
  jobs.status: "extracting" at start → "matching" on success (or "failed").
"""

from __future__ import annotations

from typing import Any

from db import client as db
from parsers.docx_parser import DocxParser
from parsers.pdf_parser import PdfParser
from retrieval.bm25_index import BM25Index


_DOCX_EXTENSIONS = {".docx", ".doc", ".dotx"}
_PDF_EXTENSIONS = {".pdf"}


async def run_track_b(
    job_id: str,
    source_file_urls: list[str],
) -> BM25Index:
    """
    B1-B5: Download sources, extract spans, write to DB, build BM25 index.
    Returns a BM25Index ready for Matching.
    Raises RuntimeError on unrecoverable failure (caller sets jobs.status=failed).
    """
    await db.set_job_status(job_id, "extracting", "Downloading and extracting source documents")

    all_span_rows: list[dict[str, Any]] = []

    for file_url in source_file_urls:
        # B1. Download
        try:
            file_bytes = await db.download_storage_file(file_url)
        except Exception as exc:
            raise RuntimeError(f"B1: Failed to download {file_url}: {exc}") from exc

        filename = file_url.split("/")[-1].split("?")[0]
        ext = _get_extension(filename)

        # B2. Route
        if ext in _DOCX_EXTENSIONS:
            spans = _extract_docx(file_bytes, filename)
        elif ext in _PDF_EXTENSIONS:
            spans = _extract_pdf(file_bytes, filename)
        else:
            # Try PDF first, fall back to DOCX
            try:
                spans = _extract_pdf(file_bytes, filename)
            except Exception:
                try:
                    spans = _extract_docx(file_bytes, filename)
                except Exception as exc2:
                    raise RuntimeError(f"B2: Unknown file type and extraction failed for {filename}: {exc2}") from exc2

        # B3. Convert to DB rows
        for span in spans:
            all_span_rows.append({
                "span_id": db.new_uuid(),
                "job_id": job_id,
                "source_document": span.source_document,
                "location": span.location,
                "raw_text": span.raw_text,
                "span_type": span.span_type,
            })

    # B5. Sanity check
    from config import settings
    if len(all_span_rows) < settings.min_span_count:
        await db.set_job_status(
            job_id,
            "failed",
            f"B5: Extraction produced only {len(all_span_rows)} spans "
            f"(minimum {settings.min_span_count} required). "
            "The source documents may be empty, image-only PDFs, or corrupt.",
        )
        raise RuntimeError(f"B5 sanity check failed: {len(all_span_rows)} spans")

    # B3. Write to DB in batches of 200
    batch_size = 200
    for i in range(0, len(all_span_rows), batch_size):
        await db.insert_many("raw_spans", all_span_rows[i : i + batch_size])

    # B4. Build BM25 index
    index = BM25Index(all_span_rows)

    await db.set_job_status(
        job_id,
        "matching",
        f"Extraction complete: {len(all_span_rows)} spans from {len(source_file_urls)} file(s)",
    )
    return index


def _get_extension(filename: str) -> str:
    import os
    return os.path.splitext(filename)[1].lower()


def _extract_docx(file_bytes: bytes, filename: str):
    parser = DocxParser(file_bytes, source_name=filename)
    return parser.extract_spans()


def _extract_pdf(file_bytes: bytes, filename: str):
    parser = PdfParser(file_bytes, source_name=filename)
    return parser.extract_spans()
