"""FastAPI app: QAC from the Lakebase prefix table, SERP from the AI Search index, demo scenarios replayed to Kafka,
and the React UI.

Runs as the app's service principal. Lakebase OAuth tokens expire hourly, so pooled connections take a fresh token
whenever they (re)connect. Both serving endpoints are route-optimized; their first call opens a connection (~400 ms),
so startup warms them in the background.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Literal

import psycopg
from databricks.sdk import WorkspaceClient
from fastapi import FastAPI, HTTPException, Query
from fastapi.staticfiles import StaticFiles
from psycopg.rows import dict_row
from psycopg_pool import ConnectionPool

from rts_app.service import latency_stats, play_scenario, qac, rows_to_dicts, search, time_calls, with_timeout
from rts_shared.config import full_name, load_config
from rts_shared.kafka import producer as kafka_producer
from rts_shared.lakebase import branch
from rts_shared.retrieval import SERP_INDEX_COLUMNS, SERP_INDEX_TEXT, qac_l2_records

ROOT = Path(__file__).resolve().parents[3]  # app/backend/rts_app/main.py -> source root
CFG = load_config(str(ROOT / os.environ.get("RTS_CONFIG", "shared/configs/dev.yml")))
LB, APP, AI = CFG["lakebase"], CFG["app"], CFG["ai_search"]
EMBED_ENDPOINT = CFG["models"]["query_embedding"]["endpoint"]
QAC_L2 = CFG["models"]["qac_l2"]
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rts_app")
QAC_L2_TIMEOUT_S = APP["timeouts_ms"]["qac_l2"] / 1000
L2_POOL = ThreadPoolExecutor(max_workers=8)  # timed-out calls finish here without holding the request
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


def qac_l2_scores(rows: list[dict], session_id: str | None, user_id: str | None) -> list[float]:
    # The endpoint looks up session and user features by key; an unknown or missing session scores as no history.
    # Runs on L2_POOL, so it finishes and logs its full time even after the request has timed out and moved on.
    t = time.perf_counter()
    try:
        return w.serving_endpoints_data_plane.query(
            name=QAC_L2["endpoint"], dataframe_records=qac_l2_records(rows, session_id, user_id)).predictions
    except Exception:
        log.exception("qac_l2 call failed")
        raise
    finally:
        log.info("qac_l2 call %.1f ms, %d candidates", (time.perf_counter() - t) * 1000, len(rows))


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


def _warm():
    try:
        embed("warm up")
        qac_l2_scores(prefix_rows("ca")[:1], None, None)
    except Exception:
        logging.exception("endpoint warm-up failed; the first requests pay the connection cost")


threading.Thread(target=_warm, daemon=True).start()


@app.get("/api/qac")
def get_qac(q: str = "", session_id: str | None = None, user_id: str | None = None):
    def rerank(rows):
        return with_timeout(L2_POOL, lambda: qac_l2_scores(rows, session_id, user_id), QAC_L2_TIMEOUT_S)

    return (qac(q, prefix_rows, LB["qac_max_prefix_len"], QAC_N, rerank, QAC_L2["candidates"])
            | {"session_id": session_id, "user_id": user_id})


@app.get("/api/search")
def get_search(q: str = "", mode: Literal["HYBRID", "ANN"] = "HYBRID", session_id: str | None = None):
    # Keyword matching covers search_text only, as in serp_l1_ai_search's recall run.
    return search(q, embed, query_index, mode, AI["serp_l1_k"], [SERP_INDEX_TEXT]) | {"session_id": session_id}


@app.get("/api/bench/qac_l2")
def bench_qac_l2(n: int = Query(100, ge=1, le=500), session_id: str | None = None, user_id: str | None = None):
    """QAC L2 call latency from inside the app, back-to-back, no timeout: 1 vs `candidates` real L1 rows for "ca"."""
    rows = prefix_rows("ca")
    sizes = sorted({1, min(QAC_L2["candidates"], len(rows))})
    return {f"candidates_{k}": latency_stats(time_calls(lambda: qac_l2_scores(rows[:k], session_id, user_id), n))
            for k in sizes} | {"session_id": session_id, "user_id": user_id}


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
