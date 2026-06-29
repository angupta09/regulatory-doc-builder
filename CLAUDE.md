# Regulatory Document Drafting Pipeline

## What this service is

Backend pipeline service for a regulatory-document drafting tool. **No UI** â€” the UI is a separate Lovable app already connected to the same Insforge project. This service owns extraction, field-matching, draft generation, and verification, exposed via exactly two HTTP endpoints.

**Two non-negotiable design principles:**
1. No value is ever written into a generated document without a citation back to an exact location in a source document. If a value can't be traced to source text, leave it blank â€” never guess, never invent.
2. Never alter the structure/format of an uploaded template. All generation edits the original `.docx` in place, cell-by-cell. Never rebuild a template layout from scratch. The only sanctioned structural change is pre-declared table-multiplication defined in schema metadata.

---

## Stack

- **Python / FastAPI** â€” `uvicorn main:app`
- **Anthropic Claude** â€” `claude-opus-4-5` for all LLM calls (matching, generation, verification)
- **pdfplumber** â€” PDF text + table extraction
- **lxml + zipfile** â€” DOCX XML-level editing (see DOCX skill rules below)
- **rank-bm25** â€” BM25 retrieval index over raw_spans (no embedding model needed)
- **httpx async** â€” Insforge PostgREST client
- **Pydantic-settings** â€” config from `.env`

---

## Environment variables (copy `.env.example` â†’ `.env`)

```
INSFORGE_API_URL=https://xxxxxxxx.us-east.insforge.app
INSFORGE_SERVICE_KEY=<service key, NOT anon key>
ANTHROPIC_API_KEY=sk-ant-...
INSFORGE_STORAGE_BUCKET=pipeline-docs
```

---

## First-run setup

```bash
pip install -r requirements.txt
cp .env.example .env          # fill in keys
python -m db.setup            # creates 7 tables + seeds starter schemas
# Upload blank templates to Insforge Storage:
#   {bucket}/templates/2.6.7_tox_summary/blank.docx
#   {bucket}/templates/csr/blank.docx
uvicorn main:app --reload
```

---

## Project structure

```
main.py                  FastAPI app â€” POST /pipeline/start, /pipeline/export, GET /health
pipeline.py              Async orchestrator: B â†’ Matching â†’ C â†’ D
config.py                Pydantic settings
requirements.txt

db/
  client.py              Async PostgREST client (httpx). insert/select/update/delete + per-table helpers.
  setup.py               Creates all 7 Insforge tables + seeds starter schema rows.

parsers/
  docx_parser.py         DocxParser (Track A2 + B3) and DocxCellWriter (Track C3).
                         XML-level via lxml â€” see DOCX skill rules below.
  pdf_parser.py          PdfParser (Track B3) â€” pdfplumber text + table extraction.

llm/
  client.py              Anthropic wrappers: enrich_schema_field (A4),
                         classify_field_from_candidates (M3),
                         generate_narrative_prose (C7),
                         verify_written_value (D3).

retrieval/
  bm25_index.py          BM25Index â€” built over raw_spans, used in Matching M2.

tracks/
  track_a.py             A1-A6: template parse â†’ LLM enrichment â†’ schemas table.
  track_b.py             B1-B5: download sources â†’ extract spans â†’ raw_spans â†’ BM25 index.
  matching.py            M1-M8: schema-driven BM25 retrieval + constrained LLM classification
                         + M4 citation gate + M7 coverage flags + M8 numeric consistency.
  track_c.py             C1-C10: XML cell writes into original template + write_log + C9 integrity.
  track_d.py             D1-D6: independent re-derivation + 4-verdict classification
                         + verification_results rows.
```

---

## Insforge / DB layer

Insforge exposes a PostgREST API. The service uses the **Service Key** (never the anon key).

```
Authorization: Bearer <INSFORGE_SERVICE_KEY>
apikey: <INSFORGE_SERVICE_KEY>
Content-Type: application/json
Prefer: return=representation
```

Base URL pattern: `{INSFORGE_API_URL}/rest/v1/{table}`

### The 7 tables

| Table | Owner | Notes |
|---|---|---|
| `jobs` | Lovable creates; pipeline updates `status`/`status_detail` | Status flow: uploading â†’ extracting â†’ matching â†’ generating â†’ verifying â†’ done / failed |
| `schemas` | `db/setup.py` creates; Track A writes | Versioned field definitions per template_type |
| `raw_spans` | Track B writes | One row per extracted paragraph or table cell. **Pipeline only â€” Lovable never writes this.** |
| `facts` | Matching writes | One row per field per job. `span_ids` = comma-separated UUIDs. |
| `write_log` | Track C writes | One row per cell written. `fact_id` links to facts. |
| `verification_results` | Track D writes; Lovable writes `reviewer_action`/`reviewer_edit_value` | One row per write_log entry, always â€” even CONFIRMED. |
| `coverage_flags` | Matching M7 writes; Lovable writes `reviewer_dismissed` | Salient spans not cited by any fact. |

### Locator format

- Paragraphs: `para:{idx}`
- Table cells: `table:{t_idx}/row:{r_idx}/col:{c_idx}` (merge-resolved, deduplicated by XML element identity)
- PDF prose: `page:{p}/line:{l}`
- PDF table cells: `page:{p}/table:{t}/row:{r}/col:{c}`

---

## Two HTTP endpoints

### `POST /pipeline/start`
```json
{ "job_id": "uuid", "template_type": "2.6.7_tox_summary|csr",
  "source_file_urls": ["..."], "template_file_url": null }
```
Returns `{ "accepted": true }` immediately. Pipeline runs as a FastAPI background task. Progress via `jobs.status` polling.

### `POST /pipeline/export`
```json
{ "job_id": "uuid" }
```
Requires all `verification_results.reviewer_action != "pending"`. Applies reviewer decisions (accepted/edited/rejected), uploads final `.docx` to Insforge Storage, returns `{ "file_url": "..." }`.

---

## Template types + starter schema fields

### `2.6.7_tox_summary`
NOAEL, Species/Strain, Method of Administration, Noteworthy Findings, Vehicle/Formulation, Number of Animals, Toxicokinetics

### `csr`
Primary Objective, Methodology, Number of Subjects (disposition), Demographics, Primary Efficacy Result, Safety Overview

---

## Pipeline flow (B â†’ Matching â†’ C â†’ D)

### Track B (extracting)
1. Download source files from Insforge Storage
2. Route by extension: `.pdf` â†’ PdfParser, `.docx` â†’ DocxParser
3. Write every extracted unit to `raw_spans` (one row per span)
4. Build BM25Index over all spans
5. Sanity check: fail if < 10 spans

### Matching
1. Load `schemas` for the template_type
2. Per field: header-string match on table_cell spans first, then BM25 retrieval (top-15)
3. **Constrained LLM**: give Claude only the retrieved candidates â€” never the full document
4. **M4 citation gate** (hard): reject any `span_id` the LLM returns that wasn't in the candidate set
5. Aggregate fields: combine multiple spans, retain all `span_ids`
6. Write `facts` rows (`matched` or `not_found`)
7. Coverage pass: flag salient uncited spans â†’ `coverage_flags`
8. Numeric consistency pre-check: `CONFLICT:` prefix in `facts.value` if mismatch found

### Track C (generating)
1. Open original template `.docx` bytes via DocxCellWriter
2. For each `matched` fact: locate cell by fieldâ†’location mapping, write value (XML-level)
3. `not_found` facts: leave cell untouched (C5 gate)
4. Log every write to `write_log`
5. C8 cross-reference check (numeric consistency across written cells)
6. C9 format integrity: verify non-target cells unchanged
7. Upload draft to `{bucket}/drafts/{job_id}/draft.docx`

### Track D (verifying)
1. Pull `write_log` for the job
2. Fetch source text **fresh from `raw_spans`** (not from memory)
3. Independent LLM call with different prompt framing than M3
4. Assign verdict: `CONFIRMED` | `MISMATCH` | `OVER_SPECIFIC` | `STALE`
5. Write one `verification_results` row per `write_log` entry â€” all of them, even CONFIRMED
6. Set `jobs.status = "done"`

---

## DOCX skill rules (critical â€” follow exactly)

A `.docx` is a ZIP archive. Never use python-docx to rebuild layouts. The correct approach for editing existing documents:

**Reading/parsing (DocxParser):**
- Unzip with `zipfile`, parse `word/document.xml` with `lxml`
- Deduplicate merged cells by Python `id(element)` â€” not by grid position (merged cells repeat the same XML element across multiple grid positions)
- Extract text via `<w:t>` nodes inside `<w:r>` runs inside `<w:p>` paragraphs

**Writing to cells (DocxCellWriter):**
- Unzip in memory, parse `word/document.xml` with lxml into a mutable tree
- Pre-index all cells as `{locator â†’ <w:tc> element}` at init time
- To write a cell: find the target `<w:tc>`, preserve existing `<w:rPr>` (run properties / formatting), replace only the `<w:t>` text content
- Keep first run's formatting; remove extra runs; if no runs exist, create a minimal `<w:r><w:t>` pair
- Add `xml:space="preserve"` to `<w:t>` if value contains leading/trailing spaces
- Reserialize with `etree.tostring(..., xml_declaration=True, encoding="UTF-8")` and rezip â€” all other files byte-for-byte identical

**Element order in `<w:pPr>`:** `<w:pStyle>`, `<w:numPr>`, `<w:spacing>`, `<w:ind>`, `<w:jc>`, `<w:rPr>` last

**Never:**
- Rebuild a table or document layout from scratch
- Use `python-docx`'s high-level API to insert paragraphs/tables (it restructures XML)
- Use `WidthType.PERCENTAGE` for table widths (breaks in Google Docs)
- Insert `\n` â€” use separate `<w:p>` elements

---

## PDF skill rules

Use **pdfplumber** for all PDF extraction:

```python
import pdfplumber

# Text with layout preservation
with pdfplumber.open(path) as pdf:
    for page in pdf.pages:
        text = page.extract_text(layout=True)
        tables = page.extract_tables()
```

- Extract tables first per page; assign `page:{p}/table:{t}/row:{r}/col:{c}` locators
- Extract prose lines with `page:{p}/line:{l}` locators
- For scanned PDFs (image-only): fall back to pytesseract OCR via `pdf2image`

---

## LLM call patterns

All calls enforce the citation rule in the system prompt. Key patterns:

**M3 matching** (`classify_field_from_candidates`):
- System: citation-strict, "answer only from the provided spans"
- User: field name + description + synonyms + candidate spans (with `span_id` labels)
- Returns JSON: `{status, value, span_ids, confidence}`
- **M4 gate**: after the call, reject any `span_id` not in the provided candidate set

**D3 verification** (`verify_written_value`):
- Deliberately different prompt framing from M3
- Given: written value + fresh source text + field name
- Returns: `{verdict, source_snippet, reasoning}`
- Verdicts: CONFIRMED / MISMATCH / OVER_SPECIFIC / STALE

**C7 prose** (`generate_narrative_prose`):
- Given only `facts` rows already in DB for this job
- Returns: `{prose, fact_ids_used}`
- Caller validates `fact_ids_used` against the provided set

All LLM responses parsed as JSON; retry up to 2x on parse failure with "return ONLY valid JSON" appended.

---

## Recommended next steps (build/test order from spec)

1. **Test Track B in isolation** â€” run against real source files, inspect `raw_spans` rows in Insforge before trusting them
2. **Test Matching in isolation** â€” check known fields (e.g., NOAEL) against ground truth for the dataset
3. **Test Track C** â€” open the resulting `.docx` and confirm only target cells changed, format untouched
4. **Test Track D** â€” deliberately introduce a dose-group misattribution and confirm it gets flagged `MISMATCH`
5. **Deploy** â€” any Python host (Railway, Fly.io, etc.); set `PIPELINE_BASE_URL` in Lovable to the deployed URL
6. **End-to-end** â€” run one real job through both Lovable pages

---

## Known gaps to address before production

- **Track A `run_track_a`** is not called automatically by the pipeline â€” it's a one-time setup. Call it manually or via a `/admin/schema` endpoint for each template type before running jobs.
- **`_resolve_location` in track_c.py** uses a simple label-match heuristic to map `field_name â†’ template cell location`. For templates with ambiguous labels, this may need refinement against the actual template structure.
- **C4 pre-declared multiplicity** (split tables lettered A/B/C) is stubbed â€” implement if the tox template requires it.
- **Storage upload** uses a simple POST to Insforge Storage; if Insforge requires multipart form upload, adjust `db/client.py:upload_storage_file`.
- **`exec_sql` RPC** in `db/setup.py` â€” if Insforge doesn't expose this endpoint, run the DDL statements manually in the Insforge SQL editor.

