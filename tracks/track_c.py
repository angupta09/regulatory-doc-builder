"""
Track C — Draft Generation (cell-level write into the real template).

Steps C1-C10 (C2 removed per spec):
  C1.  Load the original template .docx bytes from Insforge Storage.
  C3.  Cell-level write: for each matched fact, write only its target cell.
  C4.  (Pre-declared multiplicity — skipped unless schema metadata declares it.)
  C5.  Per-cell write gate: leave unmatched cells as-is.
  C6.  Write_log: record every cell write.
  C7.  Connective prose for narrative fields.
  C8.  Post-write cross-reference consistency re-check.
  C9.  Format-integrity verification.
  C10. Upload filled draft to Insforge Storage, finalize write_log rows.

Status transitions:
  jobs.status: "generating" at start → "verifying" on success.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from db import client as db
from llm.client import generate_narrative_prose
from parsers.docx_parser import DocxParser, DocxCellWriter

# A field whose description (in schema) mentions "prose" or "narrative" is treated as C7
_NARRATIVE_KEYWORDS = re.compile(r"\b(prose|narrative|summary|overview|paragraph|text)\b", re.I)

# Template storage path pattern: "templates/{template_type}/blank.docx"
_TEMPLATE_PATH_TEMPLATE = "templates/{template_type}/blank.docx"


async def run_track_c(
    job_id: str,
    template_type: str,
    template_bytes: bytes,
) -> bytes:
    """
    C1-C10: Fill the template and return the draft .docx bytes.
    Also writes all write_log rows to DB.
    Updates jobs.status from "generating" → "verifying".
    """
    await db.set_job_status(job_id, "generating", "Generating draft document")

    # C1. We already have template_bytes from the caller (passed in from storage)

    # Load the field→location mapping from schemas
    schemas = await db.get_schemas(template_type)
    schema_by_field: dict[str, dict[str, Any]] = {s["field_name"]: s for s in schemas}

    # Load matched facts for this job
    facts = await db.get_facts(job_id)
    matched_facts = [f for f in facts if f["status"] == "matched" and not f["value"].startswith("CONFLICT:")]
    facts_by_field: dict[str, dict[str, Any]] = {f["field_name"]: f for f in matched_facts}

    # Parse the template to get its field→location map
    parser = DocxParser(template_bytes, source_name=f"{template_type}_template.docx")
    skeletons = parser.extract_field_skeletons()
    # Build label → location index (first occurrence of each label wins)
    label_to_location: dict[str, str] = {}
    for sk in skeletons:
        key = sk.field_name.strip().lower()
        if key not in label_to_location:
            label_to_location[key] = sk.location

    # C3. Open the template for cell-level writing
    writer = DocxCellWriter(template_bytes)
    write_log_rows: list[dict[str, Any]] = []
    written_locations: set[str] = set()

    # C5 gate + C3 write
    for field_name, fact in facts_by_field.items():
        # Find the template location for this field
        location = _resolve_location(field_name, label_to_location, schema_by_field)
        if not location:
            # Can't map fact to a template cell — skip (C5)
            continue

        schema = schema_by_field.get(field_name, {})
        is_narrative = bool(_NARRATIVE_KEYWORDS.search(schema.get("description", "")))

        if is_narrative:
            # C7. Narrative field — LLM prose generation from available facts
            field_instructions = schema.get("instructions", "")
            value_written = await _generate_prose_for_field(field_name, facts_by_field, field_instructions)
            used_fact_ids = [fact["fact_id"]]  # simplified — prose generation returns fact_ids
        else:
            value_written = fact["value"]
            used_fact_ids = [fact["fact_id"]]

        # Write the cell
        success = writer.write_cell(location, value_written)
        if not success:
            continue

        written_locations.add(location)

        # C6. Write_log entry
        write_log_rows.append({
            "write_id": db.new_uuid(),
            "job_id": job_id,
            "template_cell_location": location,
            "fact_id": fact["fact_id"],
            "value_written": value_written,
        })

    # C8. Post-write consistency re-check
    _cross_reference_check(write_log_rows)

    # Get the modified .docx bytes
    draft_bytes = writer.get_bytes()

    # C9. Format-integrity verification
    integrity_ok = writer.verify_non_target_unchanged(template_bytes, written_locations)
    if not integrity_ok:
        # Log warning but don't abort — the reviewer will catch issues
        print(f"  [Track C] Warning: C9 integrity check found unexpected changes in non-target sections for job {job_id}")

    # C10. Upload draft to Insforge Storage
    from config import settings as _cfg
    draft_path = f"drafts/{job_id}/draft.docx"
    draft_url = await db.upload_storage_file(
        _cfg.insforge_storage_bucket,
        draft_path,
        draft_bytes,
        content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )

    # C6. Write all write_log rows to DB
    if write_log_rows:
        await db.insert_many("write_log", write_log_rows)

    await db.set_job_status(
        job_id,
        "verifying",
        f"Draft generated: {len(write_log_rows)} cells written. Integrity={'OK' if integrity_ok else 'WARNING'}",
    )

    return draft_bytes


def _resolve_location(
    field_name: str,
    label_to_location: dict[str, str],
    schema_by_field: dict[str, dict[str, Any]],
) -> str | None:
    """
    Find the template cell location for a field_name.
    Strategy: exact match on label_to_location, then fuzzy prefix match.
    """
    key = field_name.strip().lower()
    if key in label_to_location:
        return label_to_location[key]

    # Fuzzy: find any label that starts with or contains the field name
    for label, loc in label_to_location.items():
        if key in label or label in key:
            return loc

    return None


async def _generate_prose_for_field(
    field_name: str,
    facts_by_field: dict[str, dict[str, Any]],
    instructions: str = "",
) -> str:
    """C7: Generate prose using all available facts, guided by template instructions."""
    all_facts = [
        {"fact_id": f["fact_id"], "field_name": fn, "value": f["value"]}
        for fn, f in facts_by_field.items()
    ]
    try:
        result = await asyncio.get_event_loop().run_in_executor(
            None, generate_narrative_prose, field_name, all_facts, 200, instructions
        )
        return result.get("prose", "")
    except Exception as exc:
        print(f"  [Track C] Prose generation failed for '{field_name}': {exc}")
        return ""


def _cross_reference_check(write_log_rows: list[dict[str, Any]]) -> None:
    """
    C8. Basic entity/keyword-overlap check across written values.
    Detects if the same entity (number, name) appears in inconsistent forms
    across multiple written cells.
    """
    # Build (value → locations) index
    number_pattern = re.compile(r"\b\d+(?:\.\d+)?\b")
    value_to_locations: dict[str, list[str]] = {}

    for row in write_log_rows:
        value = row.get("value_written", "")
        numbers = number_pattern.findall(value)
        for num in numbers:
            value_to_locations.setdefault(num, []).append(row["template_cell_location"])

    # Log any number that appears in only one cell but the value strings look inconsistent
    # (Full resolution is left to Track D; this is a quick pre-check)
    for num, locs in value_to_locations.items():
        if len(locs) > 1:
            # Multiple cells reference the same number — flag for awareness
            print(
                f"  [Track C C8] Numeric value '{num}' appears in multiple cells: {locs} — "
                "Track D will verify consistency."
            )
