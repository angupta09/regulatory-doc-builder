"""
Anthropic LLM client wrapper.

All calls go through typed helpers that enforce:
  - A hard JSON output contract (the LLM must return valid JSON or we retry).
  - Constrained prompting — callers pass the exact candidate spans;
    the LLM cannot reference content it wasn't given.
  - System-level citations rule embedded in every call.
"""

from __future__ import annotations

import json
import re
from typing import Any

import anthropic

from config import settings

_client = anthropic.Anthropic(api_key=settings.anthropic_api_key)

MAX_RETRIES = 2
_CITATION_SYSTEM = (
    "You are a precise regulatory document analyst. "
    "You must ONLY use information explicitly present in the provided source text. "
    "Never invent, infer, or guess values. "
    "If the answer is not found in the provided text, return not_found. "
    "Every value you extract must be traceable to a specific span_id you were given."
)


def _call(
    model: str,
    system: str,
    user: str,
    max_tokens: int = 2048,
) -> str:
    """Raw call, returns assistant message text."""
    msg = _client.messages.create(
        model=model,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def _call_json(model: str, system: str, user: str, max_tokens: int = 2048) -> Any:
    """Call and parse JSON response; retry up to MAX_RETRIES on parse failure."""
    for attempt in range(MAX_RETRIES + 1):
        raw = _call(model, system, user, max_tokens)
        # Strip markdown fences if present
        cleaned = re.sub(r"^```(?:json)?\s*", "", raw.strip(), flags=re.MULTILINE)
        cleaned = re.sub(r"\s*```$", "", cleaned.strip(), flags=re.MULTILINE)
        try:
            return json.loads(cleaned)
        except json.JSONDecodeError:
            if attempt == MAX_RETRIES:
                raise ValueError(f"LLM returned non-JSON after {MAX_RETRIES + 1} attempts:\n{raw[:500]}")
            # Append clarification to user prompt on retry
            user += "\n\nIMPORTANT: Return ONLY valid JSON. No markdown, no prose."
    raise RuntimeError("unreachable")


# ---------------------------------------------------------------------------
# Track A4 — schema enrichment
# ---------------------------------------------------------------------------

def enrich_schema_field(field_name: str, location: str, context: str, template_type: str, instructions: str = "") -> dict:
    """
    Return {"description": str, "synonyms": [str, ...], "aggregate": bool}
    for a field skeleton.  If the template provided authoring instructions for
    this section, they are included to produce richer synonyms and description.
    """
    system = (
        "You are a regulatory document specialist familiar with ICH M4S, ICH E3, "
        "and standard pharmaceutical regulatory submission formats. "
        "Generate metadata for a template field to enable automated extraction from source documents."
    )
    instructions_block = f"\nAuthoring instructions from template:\n{instructions}" if instructions else ""
    user = f"""Template type: {template_type}
Field name: {field_name}
Location in template: {location}
Context (surrounding headers): {context or 'none'}{instructions_block}

Return a JSON object with:
{{
  "description": "1-2 sentence description of what this field contains in regulatory documents",
  "synonyms": ["list", "of", "alternative", "terms", "headers", "abbreviations", "used in source docs"],
  "aggregate": true/false  // true if this field typically aggregates data from multiple source spans
}}

Synonyms should be comprehensive: include abbreviations, plural forms, section headers, column names commonly found in nonclinical/clinical study reports.
Return ONLY the JSON object."""

    return _call_json(settings.llm_matching_model, system, user, max_tokens=512)


# ---------------------------------------------------------------------------
# Matching — M3 constrained classification
# ---------------------------------------------------------------------------

def classify_field_from_candidates(
    field_name: str,
    description: str,
    synonyms: list[str],
    candidates: list[dict],  # [{"span_id": ..., "location": ..., "raw_text": ...}, ...]
) -> dict:
    """
    Given only the candidate spans retrieved by BM25, extract the field value.
    Returns:
      {"status": "matched", "value": str, "span_ids": [str, ...], "confidence": str}
      OR
      {"status": "not_found", "value": "", "span_ids": [], "confidence": ""}

    The LLM is ONLY given the candidate spans — never the full document.
    Any span_id it returns must be validated by the caller against the provided set.
    """
    system = _CITATION_SYSTEM
    candidates_txt = "\n".join(
        f"[span_id={c['span_id']}] (location: {c['location']})\n{c['raw_text']}"
        for c in candidates
    )
    valid_ids = {c["span_id"] for c in candidates}

    user = f"""Extract the following field from the candidate source spans below.

Field: {field_name}
Description: {description}
Alternative terms / synonyms: {", ".join(synonyms)}

CANDIDATE SPANS (these are the ONLY sources you may use):
{candidates_txt}

Instructions:
- If you find the value, return a JSON object with:
  {{
    "status": "matched",
    "value": "<extracted value — verbatim from source, not paraphrased>",
    "span_ids": ["<span_id of primary span>", ...],
    "confidence": "high|medium|low"
  }}
- If the value is NOT present in any of the above spans, return:
  {{
    "status": "not_found",
    "value": "",
    "span_ids": [],
    "confidence": ""
  }}
- span_ids MUST be from the list above. Do not invent span IDs.
- For aggregate fields, list ALL contributing span_ids.
- Return ONLY the JSON object."""

    result = _call_json(settings.llm_matching_model, system, user, max_tokens=1024)

    # M4 citation validation — hard gate
    returned_ids = set(result.get("span_ids", []))
    invalid = returned_ids - valid_ids
    if invalid:
        # Reject: LLM hallucinated span IDs not in the candidate set
        return {
            "status": "not_found",
            "value": "",
            "span_ids": [],
            "confidence": "rejected:hallucinated_span_ids",
        }

    return result


# ---------------------------------------------------------------------------
# Track C7 — connective prose generation (narrative fields)
# ---------------------------------------------------------------------------

def generate_narrative_prose(
    field_name: str,
    facts: list[dict],  # [{"fact_id":..., "field_name":..., "value":...}, ...]
    max_words: int = 150,
    instructions: str = "",
) -> dict:
    """
    Compose a prose paragraph using ONLY the provided facts, guided by the
    template's authoring instructions for this section if available.
    Returns {"prose": str, "fact_ids_used": [str, ...]}
    """
    system = (
        "You are a regulatory medical writer. "
        "Write clear, concise regulatory prose using ONLY the data points provided. "
        "Do not introduce any number, claim, or finding not present in the input facts."
    )
    facts_txt = "\n".join(
        f"[fact_id={f['fact_id']}] {f['field_name']}: {f['value']}" for f in facts
    )
    instructions_block = (
        f"\nTemplate authoring instructions for this section:\n{instructions}\n"
        if instructions else ""
    )
    user = f"""Write a concise prose summary for the field '{field_name}' using ONLY the facts below.
Maximum {max_words} words.
{instructions_block}
AVAILABLE FACTS:
{facts_txt}

Return a JSON object:
{{
  "prose": "<the composed text>",
  "fact_ids_used": ["<fact_id>", ...]
}}
Return ONLY the JSON object."""

    result = _call_json(settings.llm_generation_model, system, user, max_tokens=512)
    valid_ids = {f["fact_id"] for f in facts}
    result["fact_ids_used"] = [fid for fid in result.get("fact_ids_used", []) if fid in valid_ids]
    return result


# ---------------------------------------------------------------------------
# Track C (narrative) — write a full section from retrieved source spans
# ---------------------------------------------------------------------------

def write_section_from_sources(
    section_name: str,
    instructions: str,
    candidates: list[dict],  # [{"span_id","location","source_document","raw_text"}, ...]
    max_words: int = 250,
) -> dict:
    """
    Compose the body of a CSR section using ONLY the provided source excerpts,
    guided by the template's authoring instructions for that section.

    Returns:
      {"content": str, "span_ids": [str, ...], "sufficient": bool}

    If the excerpts do not contain information relevant to this section,
    returns sufficient=False and empty content — the caller then leaves the
    template paragraph untouched (never invents text).
    """
    system = (
        "You are a regulatory medical writer drafting a Clinical Study Report (CSR) "
        "section. The source excerpts provided to you were already retrieved as relevant "
        "to this section — your job is to summarize the data in them that pertains to the "
        "section, in formal past-tense regulatory prose. "
        "Ground every statement in the excerpts: do NOT introduce any number, finding, name, "
        "or date not explicitly present, and do not add boilerplate or assumptions. "
        "When the excerpts contain a data table, report the concrete figures from it "
        "(group sizes, counts, percentages, key values). "
        "Only set sufficient=false if the excerpts genuinely contain NOTHING about this "
        "section's topic — do not bail merely because the data is tabular or terse."
    )
    candidates_txt = "\n\n".join(
        f"[span_id={c['span_id']}] (source: {c.get('source_document','?')}, loc: {c['location']})\n{c['raw_text']}"
        for c in candidates
    )
    instr_block = f"\nAuthoring instructions for this section (what it should contain):\n{instructions}\n" if instructions else ""

    user = f"""Write the body text for the CSR section titled: "{section_name}".
Target length: up to {max_words} words. Use only the source excerpts below.
{instr_block}
SOURCE EXCERPTS (the ONLY material you may use; cite the span_ids you draw from):
{candidates_txt}

Return ONLY a JSON object:
{{
  "content": "<the section body text, grounded entirely in the excerpts; empty string if nothing relevant>",
  "span_ids": ["<span_id actually used>", ...],
  "sufficient": true/false
}}
- sufficient=false (and content="") if the excerpts lack information for this section.
- Every factual statement in content must trace to a cited span_id.
- Do not restate the section title. Do not invent."""

    result = _call_json(settings.llm_generation_model, system, user, max_tokens=2048)

    # Strip any inline [span_id=...] citation markers the model left in the prose
    content = result.get("content", "") or ""
    content = re.sub(r"\s*\[span_id=[^\]]*\]", "", content)
    result["content"] = content.strip()

    # Citation gate: drop any span_id not in the provided candidate set
    valid_ids = {c["span_id"] for c in candidates}
    returned = result.get("span_ids", []) or []
    result["span_ids"] = [sid for sid in returned if sid in valid_ids]
    # If model claimed sufficiency but cited nothing valid, treat as insufficient
    if result.get("sufficient") and not result["span_ids"]:
        result["sufficient"] = False
        result["content"] = ""
    return result


# ---------------------------------------------------------------------------
# Track D3 — independent verification
# ---------------------------------------------------------------------------

def verify_written_value(
    written_value: str,
    source_text: str,
    source_location: str,
    field_name: str,
) -> dict:
    """
    Independent re-derivation pass (D3).
    Returns:
      {"verdict": "CONFIRMED"|"MISMATCH"|"OVER_SPECIFIC"|"STALE",
       "source_snippet": str,
       "reasoning": str}
    """
    system = (
        "You are an independent regulatory document auditor. "
        "Verify whether the written value is accurately supported by the source text. "
        "Be strict: partial matches, paraphrases, or directionally-correct but imprecise values "
        "should be flagged as OVER_SPECIFIC, not CONFIRMED."
    )
    user = f"""Verify whether the following written value is accurately supported by the source text.

Field: {field_name}
Written value: {written_value}

Source text (from {source_location}):
{source_text}

Rules:
- CONFIRMED: the source text directly and unambiguously supports the exact written value.
- MISMATCH: the source text contradicts or does not support the written value.
- OVER_SPECIFIC: the written value is directionally correct, but the source text contains a more precise figure or term that wasn't used (e.g., "approximately 100 mg/kg" when source says "98 mg/kg/day").
- STALE: (only assign if you have evidence the value references a superseded attribution — otherwise do not use this verdict).

Return a JSON object:
{{
  "verdict": "CONFIRMED"|"MISMATCH"|"OVER_SPECIFIC"|"STALE",
  "source_snippet": "<the relevant phrase from the source text, verbatim>",
  "reasoning": "<1-2 sentences explaining the verdict>"
}}
Return ONLY the JSON object."""

    return _call_json(settings.llm_verification_model, system, user, max_tokens=512)
