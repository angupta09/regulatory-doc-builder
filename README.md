# Regulatory Document Builder — Pipeline Service

Backend pipeline for a regulatory-document drafting tool. It extracts data from
source documents (protocol, SAP, TFLs, study reports), matches it to a template's
fields, generates a draft `.docx` **cell-by-cell into the original template**, and
independently verifies every written value.

The UI is a separate [Lovable](https://lovable.dev) app ("Doc Weaver") connected to
the same backend; this repo is the pipeline service it calls.

## Two non-negotiable principles

1. **Every written value is cited back to source text.** If a value can't be traced
   to a source span, the field is left blank — never guessed or invented.
2. **The uploaded template's structure/format is never altered.** Generation edits the
   original `.docx` in place; it never rebuilds a layout from scratch.

## Stack

- **Python / FastAPI** (`uvicorn main:app`)
- **Anthropic Claude** — matching & verification on Opus, narrative generation on Haiku
- **pdfplumber** (PDF) + **lxml/zipfile** (DOCX XML-level editing)
- **rank-bm25** for retrieval over extracted spans
- **Insforge** (Postgres-backed BaaS) for tables + document storage, via `httpx`

## Setup

```bash
git clone https://github.com/angupta09/regulatory-doc-builder.git
cd regulatory-doc-builder
pip install -r requirements.txt

cp .env.example .env        # then fill in YOUR keys (see below)
```

Edit `.env`:

```
INSFORGE_API_URL=https://<your-project>.insforge.app
INSFORGE_SERVICE_KEY=<your Insforge service/API key, NOT the anon key>
ANTHROPIC_API_KEY=sk-ant-...
INSFORGE_STORAGE_BUCKET=pipeline-docs
```

> **Never commit `.env`.** It holds live secrets and is gitignored. Each collaborator
> uses their own keys.

### First-run (Insforge backend)

This service expects 7 Insforge tables (`jobs`, `schemas`, `raw_spans`, `facts`,
`write_log`, `verification_results`, `coverage_flags`) and a storage bucket. With the
[Insforge CLI](https://insforge.dev) linked to your project, create the tables (DDL in
`db/setup.py`), grant API access, then seed a template schema by running Track A against
a blank template `.docx`. Upload blank templates to
`{bucket}/templates/{template_type}/blank.docx`.

> Note: this Insforge project exposes SQL at `/api/database/advance/rawsql` and storage at
> `/api/storage/buckets/{bucket}/objects/{key}` (multipart PUT) — not the standard
> Supabase `/rest/v1/` paths. See `db/client.py`.

### Run

```bash
uvicorn main:app --reload      # http://localhost:8000  (docs at /docs)
```

To let the cloud-hosted Lovable UI reach this service, it must be publicly reachable
(deploy it, or expose localhost via a tunnel) and the UI's `PIPELINE_BASE_URL` secret
set to that URL.

## HTTP endpoints

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/pipeline/start` | `{job_id, template_type, source_file_urls[]}` → runs the pipeline as a background task. Poll `jobs.status`. |
| `POST` | `/pipeline/export` | `{job_id}` → applies reviewer decisions, returns the final `.docx` URL. |
| `GET`  | `/health` | Health check. |

Template types: `csr`, `2.6.7_tox_summary`.

## Pipeline flow

**B → Matching → C → D**

- **Track B** — download sources, extract every paragraph/table cell to `raw_spans`, build a BM25 index.
- **Matching** — per template field: BM25 retrieve candidates → constrained LLM extraction → hard citation gate → `facts`.
- **Track C** — write matched values + source-grounded narrative into the template `.docx`; log to `write_log`.
- **Track D** — independent LLM re-derivation per written cell → `CONFIRMED` / `MISMATCH` / `OVER_SPECIFIC` / `STALE`.

## Project layout

```
main.py            FastAPI app (endpoints)
pipeline.py        Orchestrator: B → Matching → C → D
config.py          Settings (.env)
db/                Insforge client + table setup/seed
parsers/           DOCX (lxml) + PDF (pdfplumber) parsers
llm/               Anthropic call wrappers (citation-strict)
retrieval/         BM25 index
tracks/            track_a (schema), track_b, matching, track_c, track_d
```
