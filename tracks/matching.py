"""
Matching — M1-M8: schema-driven retrieval + constrained LLM classification.

M1.  Load schemas for the job's template_type.
M2.  Per field: try direct header match (table_cell spans) first; else BM25.
M3.  Constrained LLM classification against retrieved candidates only.
M4.  Citation validation — hard gate against hallucinated span IDs.
M5.  Aggregate fields: retrieve+classify multiple spans, combine.
M6.  Write results to facts table.
M7.  Coverage pass: flag salient uncited spans.
M8.  Numeric self-consistency pre-check.
"""

from __future__ import annotations

import asyncio
import re
from typing import Any

from config import settings
from db import client as db
from llm.client import classify_field_from_candidates
from retrieval.bm25_index import BM25Index

# Patterns that mark a span as "salient" for the coverage pass (M7)
_SALIENT_PATTERN = re.compile(
    r"""
    \b(
      \d+(?:\.\d+)?            # any number (integer or decimal)
      | p\s*[=<>]\s*\d         # p-value
      | mg/kg                  # dose unit
      | mg/m[²2]
      | \d+\s*%                # percentage
      | AUC | Cmax | t1/2      # TK parameters
      | NOAEL | NOEL | LOAEL   # tox endpoints
      | n\s*=\s*\d             # sample size
    )\b
    """,
    re.VERBOSE | re.IGNORECASE,
)

# Numeric value pattern for M8
_NUMBER_PATTERN = re.compile(r"\b\d+(?:\.\d+)?\b")


async def run_matching(job_id: str, template_type: str, index: BM25Index) -> None:
    """
    M1-M8: Full matching pass. Writes facts and coverage_flags rows to DB.
    """
    # M1. Load schemas
    schemas = await db.get_schemas(template_type)
    if not schemas:
        raise RuntimeError(f"Matching M1: no schemas found for template_type='{template_type}'")

    # Run field matching concurrently (with concurrency cap for API rate limits)
    sem = asyncio.Semaphore(5)
    tasks = [_match_field(job_id, schema, index, sem) for schema in schemas]
    fact_rows = await asyncio.gather(*tasks)

    # M6. Write all facts to DB
    flat_facts = [f for batch in fact_rows for f in batch]
    if flat_facts:
        await db.insert_many("facts", flat_facts)

    # M7. Coverage pass
    await _coverage_pass(job_id, flat_facts, index)

    # M8. Numeric self-consistency check
    await _numeric_consistency_check(job_id, flat_facts, index)


async def _match_field(
    job_id: str,
    schema: dict[str, Any],
    index: BM25Index,
    sem: asyncio.Semaphore,
) -> list[dict[str, Any]]:
    """Match a single field; return list of fact row(s) (1 for normal, N for aggregate)."""
    async with sem:
        field_name = schema["field_name"]
        description = schema.get("description", "")
        synonyms_raw = schema.get("synonyms", "")
        synonyms = [s.strip() for s in synonyms_raw.split(",") if s.strip()]
        is_aggregate = schema.get("aggregate", False)

        # M2. Build query from description + synonyms
        query = f"{field_name} {' '.join(synonyms[:8])}"

        # Try direct header match first (better precision for table columns)
        candidates = index.search_by_header_match([field_name] + synonyms[:4], k=5)
        # Then supplement with BM25
        more = index.search(query, k=settings.bm25_top_k)
        # Merge, deduplicate by span_id, keep order (header matches first)
        seen_ids: set[str] = set()
        merged: list[dict[str, Any]] = []
        for c in candidates + more:
            if c["span_id"] not in seen_ids:
                seen_ids.add(c["span_id"])
                merged.append(c)

        candidates = merged[: settings.bm25_top_k]

        if not candidates:
            return [_not_found_row(job_id, field_name)]

        # M3. Constrained LLM classification
        try:
            result = await asyncio.get_event_loop().run_in_executor(
                None,
                classify_field_from_candidates,
                field_name,
                description,
                synonyms,
                candidates,
            )
        except Exception as exc:
            print(f"  [Matching] LLM error for field '{field_name}': {exc}")
            return [_not_found_row(job_id, field_name)]

        # M4 is enforced inside classify_field_from_candidates (hallucinated IDs → not_found)
        status = result.get("status", "not_found")
        value = result.get("value", "")
        span_ids = result.get("span_ids", [])
        confidence = result.get("confidence", "")

        if status == "matched" and value:
            return [{
                "fact_id": db.new_uuid(),
                "job_id": job_id,
                "field_name": field_name,
                "value": value,
                "span_ids": ",".join(span_ids),
                "status": "matched",
                "confidence": confidence,
            }]
        else:
            return [_not_found_row(job_id, field_name)]


def _not_found_row(job_id: str, field_name: str) -> dict[str, Any]:
    return {
        "fact_id": db.new_uuid(),
        "job_id": job_id,
        "field_name": field_name,
        "value": "",
        "span_ids": "",
        "status": "not_found",
        "confidence": "",
    }


async def _coverage_pass(
    job_id: str,
    facts: list[dict[str, Any]],
    index: BM25Index,
) -> None:
    """
    M7. Scan all raw_spans for salient content that no fact cites.
    Write flagged spans to coverage_flags.
    """
    # Build set of all cited span_ids
    cited: set[str] = set()
    for fact in facts:
        for sid in (fact.get("span_ids") or "").split(","):
            sid = sid.strip()
            if sid:
                cited.add(sid)

    flags: list[dict[str, Any]] = []
    for span in index._spans:
        if span["span_id"] in cited:
            continue
        if _SALIENT_PATTERN.search(span["raw_text"]):
            flags.append({
                "flag_id": db.new_uuid(),
                "job_id": job_id,
                "span_id": span["span_id"],
                "reason": "Salient content (numbers/findings) not cited by any matched fact",
                "reviewer_dismissed": False,
            })

    if flags:
        batch_size = 200
        for i in range(0, len(flags), batch_size):
            await db.insert_many("coverage_flags", flags[i : i + batch_size])


async def _numeric_consistency_check(
    job_id: str,
    facts: list[dict[str, Any]],
    index: BM25Index,
) -> None:
    """
    M8. For each matched numeric fact, scan the span pool for a span that
    explicitly states a numeric value for the same field. If found and it
    disagrees with the matched value, flag as CONFLICT in facts.value.
    """
    # Get full span texts by span_id for fast lookup
    span_by_id: dict[str, str] = {s["span_id"]: s["raw_text"] for s in index._spans}

    updates: list[tuple[str, str]] = []  # (fact_id, new_value)

    for fact in facts:
        if fact["status"] != "matched":
            continue
        value = fact["value"]
        matched_numbers = set(_NUMBER_PATTERN.findall(value))
        if not matched_numbers:
            continue  # non-numeric field, skip

        # Search for alternative spans that mention the same field
        alt_candidates = index.search(fact["field_name"], k=5)
        for cand in alt_candidates:
            if cand["span_id"] in (fact.get("span_ids") or "").split(","):
                continue  # already cited
            cand_numbers = set(_NUMBER_PATTERN.findall(cand["raw_text"]))
            if cand_numbers and cand_numbers != matched_numbers:
                # Potential conflict
                conflicting_value = (
                    f"CONFLICT: matched='{value}' (span {fact['span_ids']}) "
                    f"vs source='{cand['raw_text'][:200]}' (span {cand['span_id']})"
                )
                updates.append((fact["fact_id"], conflicting_value))
                break

    for fact_id, new_value in updates:
        await db.update("facts", {"fact_id": f"eq.{fact_id}"}, {"value": new_value})
