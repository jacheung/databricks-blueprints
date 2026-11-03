"""FastAPI app: QAC from the Lakebase prefix table, SERP from the AI Search index, demo scenarios replayed to Kafka,
and the React UI.

Runs as the app's service principal. Lakebase OAuth tokens expire hourly, so pooled connections take a fresh token
whenever they (re)connect.
"""

from __future__ import annotations

import os
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from rts_app.service import play_scenario, qac, rows_to_dicts, search
from rts_shared.config import full_name, load_config
from rts_shared.kafka import producer as kafka_producer
from rts_shared.lakebase import branch
from rts_shared.retrieval import SERP_INDEX_COLUMNS, SERP_INDEX_TEXT

ROOT = Path(__file__).resolve().parents[3]  # app/backend/rts_app/main.py -> source root
CFG = load_config(str(ROOT / os.environ.get("RTS_CONFIG", "shared/configs/dev.yml")))
LB, APP, AI = CFG["lakebase"], CFG["app"], CFG["ai_search"]
EMBED_ENDPOINT = CFG["models"]["query_embedding"]["endpoint"]
SCHEMA = CFG["unity_catalog"]["schema"]
QAC_N = 10
w = WorkspaceClient()

_endpoint = next(e for e in w.postgres.list_endpoints(parent=branch(CFG))
                 if "READ_WRITE" in str(e.status.endpoint_type))
_token = {"value": None, "at": 0.0}


def _password() -> str:
    if time.time() - _token["at"] > 45 * 60:  # tokens last an hour
        _token.update(value=w.postgres.generate_database_credential(endpoint=_endpoint.name).token, at=time.time())
    return _token["value"]


class _TokenConnection(psycopg.Connection):
    @classmethod
    def connect(cls, conninfo: str = "", **kwargs):
        kwargs["password"] = _password()
        return super().connect(conninfo, **kwargs)


# PGUSER is the app's Postgres role, injected by the app's Lakebase resource.
pool = ConnectionPool(
    kwargs={"host": _endpoint.status.hosts.host, "dbname": LB["database"], "user": os.environ["PGUSER"],
            "sslmode": "require", "autocommit": True, "row_factory": dict_row},
    connection_class=_TokenConnection, min_size=2, max_size=8, open=True)


def _table(key: str) -> str:
    return f'"{SCHEMA}"."{CFG["tables"][key]}"'


def prefix_rows(prefix: str) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(f"SELECT * FROM {_table('qac_prefix_online')} WHERE prefix = %s ORDER BY rank",
                            (prefix,)).fetchall()


def embed(text: str) -> list[float]:
    # Route-optimized endpoint: the data-plane client uses the app SP's OAuth token.
    return w.serving_endpoints_data_plane.query(name=EMBED_ENDPOINT, dataframe_records=[{"text": text}]).predictions[0]


def query_index(**kw) -> list[dict]:
    r = w.vector_search_indexes.query_index(index_name=full_name(CFG, AI["index"]), columns=list(SERP_INDEX_COLUMNS),
                                            **kw)
    return rows_to_dicts([c.name for c in r.manifest.columns], r.result.data_array)


def scenarios() -> dict[str, dict]:
    with pool.connection() as conn:
        rows = conn.execute(f"SELECT * FROM {_table('demo_scenarios_online')} ORDER BY scenario_id").fetchall()
    return {r["scenario_id"]: r for r in rows}


_producer = None


def producer():
    global _producer
    if _producer is None:
        _producer = kafka_producer(CFG, w)
    return _producer


app = FastAPI(title="real-time-search")


@app.get("/api/config")
def config():
    return {"qac_debounce_ms": APP["qac_debounce_ms"], "qac_n": QAC_N}


@app.get("/api/qac")
def get_qac(q: str = "", session_id: str | None = None):
    return qac(q, prefix_rows, LB["qac_max_prefix_len"], QAC_N) | {"session_id": session_id}


@app.get("/api/search")
def get_search(q: str = "", mode: Literal["HYBRID", "ANN"] = "HYBRID", session_id: str | None = None):
    # Keyword matching covers search_text only, as in serp_l1_ai_search's recall run.
    return search(q, embed, query_index, mode, AI["serp_l1_k"], [SERP_INDEX_TEXT]) | {"session_id": session_id}


@app.get("/api/scenarios")
def list_scenarios():
    return [{k: s[k] for k in ("scenario_id", "label", "user_id", "next_query")} for s in scenarios().values()]


@app.post("/api/scenarios/{scenario_id}/play")
def play(scenario_id: str):
    scenario = scenarios().get(scenario_id)
    if scenario is None:
        raise HTTPException(404, f"no scenario {scenario_id}")
    session_id = f"demo-{uuid.uuid4().hex[:10]}"
    n = play_scenario(scenario, session_id, producer(), CFG["kafka"]["topic"], datetime.now(timezone.utc))
    return {"session_id": session_id, "user_id": scenario["user_id"], "events": n,
            "next_query": scenario["next_query"]}


_dist = ROOT / "app" / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
