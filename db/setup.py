"""
Create the 7 Insforge tables via PostgREST RPC or direct SQL.

Insforge exposes a /rest/v1/rpc/exec_sql endpoint (or similar) when the
service_role key is used.  If that's not available, this script falls back
to printing DDL that can be run manually in the Insforge SQL editor.

Run once:  python -m db.setup
"""

from __future__ import annotations

import asyncio
import sys

import httpx

from config import settings

DDL_STATEMENTS = [
    # jobs
    """
    CREATE TABLE IF NOT EXISTS jobs (
        job_id        UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        template_type TEXT NOT NULL,
        status        TEXT NOT NULL DEFAULT 'uploading',
        status_detail TEXT NOT NULL DEFAULT '',
        created_at    TIMESTAMPTZ NOT NULL DEFAULT now()
    );
    """,
    # schemas
    """
    CREATE TABLE IF NOT EXISTS schemas (
        schema_id     UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        template_type TEXT NOT NULL,
        field_name    TEXT NOT NULL,
        description   TEXT NOT NULL DEFAULT '',
        synonyms      TEXT NOT NULL DEFAULT '',
        aggregate     BOOLEAN NOT NULL DEFAULT false,
        version       INTEGER NOT NULL DEFAULT 1
    );
    """,
    # raw_spans
    """
    CREATE TABLE IF NOT EXISTS raw_spans (
        span_id         UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id          UUID NOT NULL REFERENCES jobs(job_id),
        source_document TEXT NOT NULL,
        location        TEXT NOT NULL,
        raw_text        TEXT NOT NULL,
        span_type       TEXT NOT NULL DEFAULT 'prose'
    );
    """,
    # facts
    """
    CREATE TABLE IF NOT EXISTS facts (
        fact_id    UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id     UUID NOT NULL REFERENCES jobs(job_id),
        field_name TEXT NOT NULL,
        value      TEXT NOT NULL DEFAULT '',
        span_ids   TEXT NOT NULL DEFAULT '',
        status     TEXT NOT NULL DEFAULT 'not_found',
        confidence TEXT NOT NULL DEFAULT ''
    );
    """,
    # write_log
    """
    CREATE TABLE IF NOT EXISTS write_log (
        write_id              UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id                UUID NOT NULL REFERENCES jobs(job_id),
        template_cell_location TEXT NOT NULL,
        fact_id               UUID REFERENCES facts(fact_id),
        value_written         TEXT NOT NULL DEFAULT ''
    );
    """,
    # verification_results
    """
    CREATE TABLE IF NOT EXISTS verification_results (
        verification_id  UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        write_id         UUID NOT NULL REFERENCES write_log(write_id),
        verdict          TEXT NOT NULL,
        source_snippet   TEXT NOT NULL DEFAULT '',
        reasoning        TEXT NOT NULL DEFAULT '',
        reviewer_action  TEXT NOT NULL DEFAULT 'pending',
        reviewer_edit_value TEXT
    );
    """,
    # coverage_flags
    """
    CREATE TABLE IF NOT EXISTS coverage_flags (
        flag_id            UUID PRIMARY KEY DEFAULT gen_random_uuid(),
        job_id             UUID NOT NULL REFERENCES jobs(job_id),
        span_id            UUID NOT NULL REFERENCES raw_spans(span_id),
        reason             TEXT NOT NULL DEFAULT '',
        reviewer_dismissed BOOLEAN NOT NULL DEFAULT false
    );
    """,
]


async def run_ddl(sql: str) -> dict:
    """Execute DDL via PostgREST rpc/query endpoint."""
    base = settings.insforge_api_url.rstrip("/")
    headers = {
        "Authorization": f"Bearer {settings.insforge_service_key}",
        "apikey": settings.insforge_service_key,
        "Content-Type": "application/json",
    }
    # Try Supabase-style rpc endpoint
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{base}/rest/v1/rpc/exec_sql",
            json={"query": sql},
            headers=headers,
        )
        return {"status": resp.status_code, "body": resp.text}


async def create_tables() -> None:
    print("Creating Insforge tables...\n")
    for ddl in DDL_STATEMENTS:
        clean = " ".join(ddl.split())
        table_name = clean.split("EXISTS")[1].split("(")[0].strip()
        result = await run_ddl(ddl)
        if result["status"] in (200, 201, 204):
            print(f"  OK {table_name}")
        else:
            print(f"  FAIL {table_name}  ->  HTTP {result['status']}: {result['body'][:120]}")
            print("\n  If exec_sql RPC is not enabled, run this DDL manually in the Insforge SQL editor:")
            for stmt in DDL_STATEMENTS:
                print(stmt)
            sys.exit(1)
    print("\nAll tables created.")


async def seed_schemas() -> None:
    """Insert the starter schema rows for both template types."""
    from db.client import insert_many, select

    starter: list[dict] = [
        # â”€â”€ 2.6.7 Toxicology Summary â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "NOAEL",
            "description": "No-observed-adverse-effect level for the study, typically expressed in mg/kg/day or mg/kg.",
            "synonyms": "no observed adverse effect level,no-observed-adverse-effect level,NOAEL,NOEL,no-effect level,no observed effect level",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "Species/Strain",
            "description": "Animal species and strain used in the toxicology study (e.g., Sprague-Dawley rat, beagle dog).",
            "synonyms": "species,strain,animal model,test species,rat,mouse,dog,monkey,rabbit,guinea pig,Sprague-Dawley,Wistar,CD-1,C57BL/6",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "Method of Administration",
            "description": "Route by which the test article was administered (e.g., oral gavage, intravenous, subcutaneous).",
            "synonyms": "route of administration,route,dosing route,administration route,oral,IV,intravenous,subcutaneous,SC,intramuscular,IM,inhalation,gavage,dietary,topical",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "Noteworthy Findings",
            "description": "Key toxicological findings including target organs, histopathological changes, clinical signs, and dose-response relationships.",
            "synonyms": "findings,notable findings,key findings,principal findings,toxicological findings,histopathology,clinical signs,adverse effects,target organ,organ toxicity,toxicity findings,pathology",
            "aggregate": True,
            "version": 1,
        },
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "Vehicle/Formulation",
            "description": "Vehicle or formulation used to administer the test article.",
            "synonyms": "vehicle,formulation,excipient,carrier,diluent,solvent,suspension,solution,dosing formulation",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "Number of Animals",
            "description": "Number of animals per group and/or total, often broken down by sex and dose group.",
            "synonyms": "number of animals,n per group,animals per group,group size,N,animal count,number per sex,males,females",
            "aggregate": True,
            "version": 1,
        },
        {
            "template_type": "2.6.7_tox_summary",
            "field_name": "Toxicokinetics",
            "description": "Toxicokinetic parameters measured in the study such as Cmax, AUC, t1/2, Tmax at various dose levels.",
            "synonyms": "TK,toxicokinetics,TK parameters,Cmax,AUC,AUC0-t,AUC0-inf,t1/2,half-life,Tmax,bioavailability,exposure,systemic exposure,plasma concentration",
            "aggregate": True,
            "version": 1,
        },
        # â”€â”€ CSR Synopsis â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€â”€
        {
            "template_type": "csr",
            "field_name": "Primary Objective",
            "description": "The primary objective or aim of the clinical study.",
            "synonyms": "primary objective,main objective,study objective,primary aim,purpose of the study,primary endpoint objective,primary goal",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "csr",
            "field_name": "Methodology",
            "description": "Overall study design including type (randomised, double-blind, placebo-controlled), phases, and key design elements.",
            "synonyms": "methodology,study design,design,methods,study type,randomised controlled trial,RCT,double-blind,open-label,crossover,parallel group,phase 1,phase 2,phase 3",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "csr",
            "field_name": "Number of Subjects (disposition)",
            "description": "Number of subjects screened, enrolled, randomised, treated, completed, and discontinued.",
            "synonyms": "number of subjects,subject disposition,patients enrolled,randomised patients,screened,enrolled,completed,discontinued,withdrawn,ITT,mITT,safety population,per protocol",
            "aggregate": True,
            "version": 1,
        },
        {
            "template_type": "csr",
            "field_name": "Demographics",
            "description": "Baseline demographic characteristics of the study population including age, sex, race, weight.",
            "synonyms": "demographics,baseline characteristics,age,sex,gender,race,ethnicity,weight,BMI,baseline demographics,patient characteristics",
            "aggregate": True,
            "version": 1,
        },
        {
            "template_type": "csr",
            "field_name": "Primary Efficacy Result",
            "description": "Results for the primary efficacy endpoint, including point estimates, confidence intervals, and p-values.",
            "synonyms": "primary efficacy,primary outcome,primary result,efficacy result,primary endpoint result,treatment effect,p-value,confidence interval,CI,odds ratio,hazard ratio,relative risk,mean difference",
            "aggregate": False,
            "version": 1,
        },
        {
            "template_type": "csr",
            "field_name": "Safety Overview",
            "description": "Overview of safety and tolerability including incidence of adverse events, serious adverse events, and discontinuations due to AEs.",
            "synonyms": "safety,tolerability,adverse events,AEs,SAEs,serious adverse events,adverse drug reactions,ADRs,deaths,discontinuations,safety summary,safety profile",
            "aggregate": True,
            "version": 1,
        },
    ]

    # Check if already seeded
    existing = await select("schemas", {"template_type": "eq.2.6.7_tox_summary"})
    if existing:
        print(f"  Schemas already seeded ({len(existing)} rows for 2.6.7_tox_summary).")
        return

    await insert_many("schemas", starter)
    print(f"  Seeded {len(starter)} schema rows.")


async def main() -> None:
    await create_tables()
    print("\nSeeding starter schemas...")
    await seed_schemas()
    print("\nSetup complete.")


if __name__ == "__main__":
    asyncio.run(main())

