"""
Insforge async database client — uses the /api/database/advance/rawsql endpoint
since Insforge does not expose a standard PostgREST /rest/v1/ interface.

All queries are parameterised SQL strings executed via POST to the rawsql endpoint.
The service API key is used for all calls.
"""

from __future__ import annotations

import json
import uuid
from typing import Any

import httpx

from config import settings


def _headers() -> dict[str, str]:
    return {
        "Authorization": f"Bearer {settings.insforge_service_key}",
        "apikey": settings.insforge_service_key,
        "Content-Type": "application/json",
    }


def _rawsql_url() -> str:
    base = settings.insforge_api_url.rstrip("/")
    return f"{base}/api/database/advance/rawsql"


async def _query(sql: str) -> list[dict[str, Any]]:
    """Execute SQL and return rows as list of dicts."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            _rawsql_url(),
            json={"query": sql},
            headers=_headers(),
        )
        resp.raise_for_status()
        data = resp.json()
        rows = data.get("rows") or data.get("data") or []
        return rows


def _val(v: Any) -> str:
    """Render a Python value as a SQL literal."""
    if v is None:
        return "NULL"
    if isinstance(v, bool):
        return "TRUE" if v else "FALSE"
    if isinstance(v, (int, float)):
        return str(v)
    # Escape single quotes by doubling them
    escaped = str(v).replace("'", "''")
    return f"'{escaped}'"


# ---------------------------------------------------------------------------
# Generic CRUD helpers
# ---------------------------------------------------------------------------

async def insert(table: str, row: dict[str, Any]) -> dict[str, Any]:
    cols = ", ".join(row.keys())
    vals = ", ".join(_val(v) for v in row.values())
    sql = f"INSERT INTO {table} ({cols}) VALUES ({vals}) RETURNING *;"
    rows = await _query(sql)
    return rows[0] if rows else {}


async def insert_many(table: str, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not rows:
        return []
    cols = ", ".join(rows[0].keys())
    val_rows = ", ".join(
        "(" + ", ".join(_val(v) for v in row.values()) + ")"
        for row in rows
    )
    sql = f"INSERT INTO {table} ({cols}) VALUES {val_rows} RETURNING *;"
    return await _query(sql)


async def select(
    table: str,
    filters: dict[str, str] | None = None,
    columns: str = "*",
    order: str | None = None,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """
    Select rows. filters uses PostgREST-style values like {"job_id": "eq.uuid"}.
    Translated to SQL WHERE clauses.
    """
    sql = f"SELECT {columns} FROM {table}"
    where_clauses = []
    if filters:
        for col, val in filters.items():
            # Skip join-style filters (e.g. "write_log.job_id") — not needed for rawsql
            if "." in col and not col.startswith(table):
                continue
            # Parse PostgREST operator prefix
            if "." in val:
                op_str, operand = val.split(".", 1)
            else:
                op_str, operand = "eq", val

            op_map = {
                "eq": "=", "neq": "!=", "lt": "<", "lte": "<=",
                "gt": ">", "gte": ">=", "like": "LIKE", "ilike": "ILIKE",
            }
            op = op_map.get(op_str, "=")
            where_clauses.append(f"{col} {op} {_val(operand)}")

    if where_clauses:
        sql += " WHERE " + " AND ".join(where_clauses)
    if order:
        # Convert "field_name.asc" → "field_name ASC"
        order_sql = order.replace(".asc", " ASC").replace(".desc", " DESC")
        sql += f" ORDER BY {order_sql}"
    if limit is not None:
        sql += f" LIMIT {limit}"
    sql += ";"
    return await _query(sql)


async def update(
    table: str,
    filters: dict[str, str],
    patch: dict[str, Any],
) -> list[dict[str, Any]]:
    set_clause = ", ".join(f"{k} = {_val(v)}" for k, v in patch.items())
    where_clauses = []
    for col, val in filters.items():
        if "." in val:
            op_str, operand = val.split(".", 1)
        else:
            op_str, operand = "eq", val
        op_map = {"eq": "=", "neq": "!="}
        op = op_map.get(op_str, "=")
        where_clauses.append(f"{col} {op} {_val(operand)}")
    where = " AND ".join(where_clauses)
    sql = f"UPDATE {table} SET {set_clause} WHERE {where} RETURNING *;"
    return await _query(sql)


async def delete(table: str, filters: dict[str, str]) -> None:
    where_clauses = []
    for col, val in filters.items():
        if "." in val:
            op_str, operand = val.split(".", 1)
        else:
            op_str, operand = "eq", val
        op_map = {"eq": "=", "neq": "!="}
        op = op_map.get(op_str, "=")
        where_clauses.append(f"{col} {op} {_val(operand)}")
    where = " AND ".join(where_clauses)
    sql = f"DELETE FROM {table} WHERE {where};"
    await _query(sql)


# ---------------------------------------------------------------------------
# Convenience wrappers for each table
# ---------------------------------------------------------------------------

async def set_job_status(job_id: str, status: str, detail: str = "") -> None:
    await update(
        "jobs",
        {"job_id": f"eq.{job_id}"},
        {"status": status, "status_detail": detail},
    )


async def get_job(job_id: str) -> dict[str, Any] | None:
    rows = await select("jobs", {"job_id": f"eq.{job_id}"})
    return rows[0] if rows else None


async def get_schemas(template_type: str) -> list[dict[str, Any]]:
    return await select(
        "schemas",
        {"template_type": f"eq.{template_type}"},
        order="field_name.asc",
    )


async def get_raw_spans(job_id: str) -> list[dict[str, Any]]:
    return await select("raw_spans", {"job_id": f"eq.{job_id}"})


async def get_facts(job_id: str) -> list[dict[str, Any]]:
    return await select("facts", {"job_id": f"eq.{job_id}"})


async def get_write_log(job_id: str) -> list[dict[str, Any]]:
    return await select("write_log", {"job_id": f"eq.{job_id}"})


async def get_verification_results(job_id: str) -> list[dict[str, Any]]:
    """Return verification_results joined through write_log for a job."""
    sql = (
        f"SELECT vr.* FROM verification_results vr "
        f"JOIN write_log wl ON vr.write_id = wl.write_id "
        f"WHERE wl.job_id = '{job_id}';"
    )
    return await _query(sql)


async def download_storage_file(file_url: str) -> bytes:
    """Download a file from Insforge Storage by its URL."""
    async with httpx.AsyncClient(timeout=120, follow_redirects=True) as client:
        headers = {
            "Authorization": f"Bearer {settings.insforge_service_key}",
            "apikey": settings.insforge_service_key,
        }
        resp = await client.get(file_url, headers=headers)
        resp.raise_for_status()
        return resp.content


async def upload_storage_file(bucket: str, path: str, content: bytes, content_type: str = "application/octet-stream") -> str:
    """
    Upload bytes to Insforge Storage, return the public URL.

    Insforge expects a multipart/form-data PUT with field name "file"
    (matching the @insforge/cli behaviour) — not a raw-bytes body.
    Do NOT set Content-Type manually; httpx adds the multipart boundary.
    """
    from urllib.parse import quote
    base = settings.insforge_api_url.rstrip("/")
    object_key = quote(path, safe="")
    url = f"{base}/api/storage/buckets/{bucket}/objects/{object_key}"
    headers = {
        "Authorization": f"Bearer {settings.insforge_service_key}",
        "apikey": settings.insforge_service_key,
    }
    files = {"file": (path, content, content_type)}
    async with httpx.AsyncClient(timeout=120) as client:
        resp = await client.put(url, files=files, headers=headers)
        resp.raise_for_status()
        return f"{base}/api/storage/buckets/{bucket}/objects/{object_key}"


def new_uuid() -> str:
    return str(uuid.uuid4())
