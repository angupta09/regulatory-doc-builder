"""
Track A — Template Schema Generation (one-time per template type).

Steps A1-A6 (A5 removed per spec):
  A1. Accept blank template file bytes.
  A2. Deterministic structural parse via DocxParser.
  A3. (No external guidance text in this implementation — handled inline via LLM context.)
  A4. LLM-drafted semantic layer (descriptions, synonyms, aggregate flag).
  A6. Store versioned rows in `schemas` table.
"""

from __future__ import annotations

import asyncio
from typing import Any

from db.client import select, insert_many, new_uuid
from llm.client import enrich_schema_field
from parsers.docx_parser import DocxParser, FieldSkeleton


async def run_track_a(
    template_bytes: bytes,
    template_type: str,
    template_filename: str = "template.docx",
    force_reseed: bool = False,
) -> list[dict[str, Any]]:
    """
    A1-A6: Parse template, enrich fields with LLM, store schemas.

    Returns the list of schema rows written to DB.
    If schemas already exist for this template_type and force_reseed=False,
    returns the existing rows without re-processing.
    """
    # Check if already exists
    existing = await select("schemas", {"template_type": f"eq.{template_type}"})
    if existing and not force_reseed:
        return existing

    # A2. Structural parse
    parser = DocxParser(template_bytes, source_name=template_filename)
    skeletons: list[FieldSkeleton] = parser.extract_field_skeletons()

    # Deduplicate by field_name (template cells often have duplicate labels)
    seen_names: set[str] = set()
    unique_skeletons: list[FieldSkeleton] = []
    for sk in skeletons:
        key = sk.field_name.lower().strip()
        if key not in seen_names and len(key) > 2:
            seen_names.add(key)
            unique_skeletons.append(sk)

    # A4. LLM enrichment (run concurrently with a semaphore to avoid rate limits)
    schema_rows: list[dict[str, Any]] = []
    sem = asyncio.Semaphore(5)

    async def enrich_one(sk: FieldSkeleton) -> dict[str, Any] | None:
        async with sem:
            try:
                enriched = await asyncio.get_event_loop().run_in_executor(
                    None,
                    enrich_schema_field,
                    sk.field_name,
                    sk.location,
                    sk.context,
                    template_type,
                    sk.instructions,
                )
                synonyms_raw = enriched.get("synonyms", [])
                if isinstance(synonyms_raw, list):
                    synonyms_str = ",".join(synonyms_raw)
                else:
                    synonyms_str = str(synonyms_raw)

                return {
                    "schema_id": new_uuid(),
                    "template_type": template_type,
                    "field_name": sk.field_name,
                    "description": enriched.get("description", ""),
                    "synonyms": synonyms_str,
                    "aggregate": bool(enriched.get("aggregate", False)),
                    "instructions": sk.instructions,
                    "version": 1,
                }
            except Exception as exc:
                # Don't abort the whole track for one bad field
                print(f"  [Track A] Warning: enrichment failed for '{sk.field_name}': {exc}")
                return {
                    "schema_id": new_uuid(),
                    "template_type": template_type,
                    "field_name": sk.field_name,
                    "description": "",
                    "synonyms": "",
                    "aggregate": False,
                    "instructions": sk.instructions,
                    "version": 1,
                }

    tasks = [enrich_one(sk) for sk in unique_skeletons]
    results = await asyncio.gather(*tasks)
    schema_rows = [r for r in results if r is not None]

    if not schema_rows:
        raise RuntimeError("Track A: no schema fields extracted from template")

    # A6. Store (delete old version if force_reseed)
    if force_reseed and existing:
        from db.client import delete as db_delete
        await db_delete("schemas", {"template_type": f"eq.{template_type}"})

    await insert_many("schemas", schema_rows)
    return schema_rows
