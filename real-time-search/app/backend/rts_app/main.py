"""FastAPI app: QAC from the Lakebase prefix table, SERP from the AI Search index, demo scenarios replayed to Kafka,
and the React UI.

Runs as the app's service principal. Lakebase OAuth tokens expire hourly, so pooled connections take a fresh token
whenever they (re)connect. Both serving endpoints are route-optimized; their first call opens a connection (~400 ms),
so startup warms them in the background.
"""

from __future__ import annotations

import itertools
import itertools
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

from rts_app.service import (latency_stats, play_scenario, qac, rows_to_dicts, scenario_timeline, search,
                             expected_session_features, freshness_breakdown, l2_inputs, landed, time_calls,
                             user_feature_values, with_timeout, RecentValues, SESSION_TABLES)
from rts_shared.config import full_name, load_config
from rts_shared.kafka import producer as kafka_producer
from rts_shared.lakebase import branch, features_database
from rts_shared.retrieval import SERP_INDEX_COLUMNS, SERP_INDEX_TEXT, qac_l2_records, serp_l2_records

ROOT = Path(__file__).resolve().parents[3]  # app/backend/rts_app/main.py -> source root
CFG = load_config(str(ROOT / os.environ.get("RTS_CONFIG", "shared/configs/dev.yml")))
LB, APP, AI = CFG["lakebase"], CFG["app"], CFG["ai_search"]
EMBED_ENDPOINT = CFG["models"]["query_embedding"]["endpoint"]
QAC_L2 = CFG["models"]["qac_l2"]
SERP_L2_ENDPOINT = CFG["models"]["serp_l2"]["endpoint"]
logging.basicConfig(level=logging.INFO)
log = logging.getLogger("rts_app")
QAC_L2_TIMEOUT_S = APP["timeouts_ms"]["qac_l2"] / 1000
SERP_L2_TIMEOUT_S = APP["timeouts_ms"]["serp_l2"] / 1000
L2_POOL = ThreadPoolExecutor(max_workers=8)  # timed-out calls finish here without holding the request
SCHEMA = CFG["unity_catalog"]["schema"]
QAC_N = 10
SERP_N = 40  # 4 UI pages of 10; L2 reranks every L1 hit either way, so paging needs no extra call
L2_INPUTS = l2_inputs(CFG)
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
# Feature Store online tables are in their own database, named after the catalog (rts_shared.lakebase).
FEATURES_DB = features_database(CFG)
features_pool = ConnectionPool(
    kwargs={"host": _endpoint.status.hosts.host, "dbname": FEATURES_DB, "user": os.environ["PGUSER"],
            "sslmode": "require", "autocommit": True, "row_factory": dict_row},
    connection_class=_TokenConnection, min_size=1, max_size=4, open=True)


def _table(key: str) -> str:
    return f'"{SCHEMA}"."{CFG["tables"][key]}"'


def prefix_rows(prefix: str) -> list[dict]:
    with pool.connection() as conn:
        return conn.execute(f"SELECT * FROM {_table('qac_prefix_online')} WHERE prefix = %s ORDER BY rank",
                            (prefix,)).fetchall()


def embed(text: str) -> list[float]:
    # Route-optimized endpoint: the data-plane client uses the app SP's OAuth token.
    return w.serving_endpoints_data_plane.query(name=EMBED_ENDPOINT, dataframe_records=[{"text": text}]).predictions[0]


L2_TIMES = RecentValues()  # full L2 call time per /api/qac request, read by the UI after a timeout
PLAYED = RecentValues(200)  # what each Play sent, so /freshness knows when every table has caught up
FRESHNESS_POLL_S, FRESHNESS_GIVE_UP_S = 0.01, 15


def qac_l2_scores(rows: list[dict], session_id: str | None, user_id: str | None,
                  request_id: str | None = None) -> list[float]:
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
        ms = (time.perf_counter() - t) * 1000
        log.info("qac_l2 call %.1f ms, %d candidates", ms, len(rows))
        if request_id:
            L2_TIMES.record(request_id, round(ms, 2))


def serp_l2_scores(hits: list[dict], session_id: str | None, user_id: str | None) -> list[float]:
    # Same lookups as QAC L2 (session and user features by key), plus item features by product_id. Runs on L2_POOL.
    t = time.perf_counter()
    try:
        scores = w.serving_endpoints_data_plane.query(
            name=SERP_L2_ENDPOINT, dataframe_records=serp_l2_records(hits, session_id, user_id)).predictions
    except Exception:
        log.exception("serp_l2 call failed")
        raise
    finally:
        log.info("serp_l2 call %.1f ms, %d candidates", (time.perf_counter() - t) * 1000, len(hits))
    if len(scores) != len(hits):
        raise ValueError(f"serp_l2 returned {len(scores)} scores for {len(hits)} hits")
    return scores


def query_index(**kw) -> list[dict]:
    r = w.vector_search_indexes.query_index(index_name=full_name(CFG, AI["index"]), columns=list(SERP_INDEX_COLUMNS),
                                            **kw)
    return rows_to_dicts([c.name for c in r.manifest.columns], r.result.data_array)


_session_tables, _user_tables = {}, {}


def _online_tables(prefix: str, columns: dict[str, str], cache: dict) -> dict[str, str]:
    """Feature View online tables by kind. Feature Views names them with a generated suffix that changes if the
    features are re-materialized, so each is looked up by a feature only it holds rather than set in config."""
    if not cache:
        # pg_catalog, not information_schema: the latter hides tables this role has no privilege on.
        with features_pool.connection() as conn:
            found = conn.execute(
                "SELECT n.nspname, c.relname, a.attname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid = c.oid "
                "WHERE n.nspname = %s AND c.relkind IN ('r', 'p') AND c.relname LIKE %s "
                "AND a.attname = ANY(%s)", (SCHEMA, f"{prefix}%", list(columns.values()))).fetchall()
        for kind, column in columns.items():
            names = [f'"{r["nspname"]}"."{r["relname"]}"' for r in found if r["attname"] == column]
            if len(names) != 1:
                raise HTTPException(503, f"expected one {prefix} table with {column} in {FEATURES_DB}, found {names}")
            cache[kind] = names[0]
    return cache


def session_tables() -> dict[str, str]:
    return _online_tables("rts_session_online", SESSION_TABLES, _session_tables)


def user_tables() -> dict[str, str]:
    return _online_tables("rts_user_online", {f: f for f in L2_INPUTS["batch"]}, _user_tables)


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
    return {"qac_debounce_ms": APP["qac_debounce_ms"], "qac_n": QAC_N, "l2_inputs": L2_INPUTS,
            "serp_budget_ms": CFG["budgets_ms"]["serp_total_p95"]}


def _warm():
    try:
        embed("warm up")
        qac_l2_scores(prefix_rows("ca")[:1], None, None)
        serp_l2_scores(query_index(query_type="ANN", query_vector=embed("warm up"), num_results=1), None, None)
    except Exception:
        logging.exception("endpoint warm-up failed; the first requests pay the connection cost")


threading.Thread(target=_warm, daemon=True).start()


@app.get("/api/qac")
def get_qac(q: str = "", session_id: str | None = None, user_id: str | None = None):
    request_id = uuid.uuid4().hex[:12]

    def rerank(rows):
        return with_timeout(L2_POOL, lambda: qac_l2_scores(rows, session_id, user_id, request_id), QAC_L2_TIMEOUT_S)

    return (qac(q, prefix_rows, LB["qac_max_prefix_len"], QAC_N, rerank, QAC_L2["candidates"])
            | {"session_id": session_id, "user_id": user_id, "request_id": request_id})


@app.get("/api/qac/{request_id}/l2_time")
def qac_l2_time(request_id: str):
    """The L2 call's full time, including any time after the request timed out; 404 while it's still running."""
    ms = L2_TIMES.get(request_id)
    if ms is None:
        raise HTTPException(404, "L2 call still running or unknown")
    return {"l2_full_ms": ms}


@app.get("/api/search")
def get_search(q: str = "", mode: Literal["HYBRID", "ANN"] = "HYBRID", session_id: str | None = None,
               user_id: str | None = None):
    # Keyword matching covers search_text only, as in serp_l1_ai_search's recall run.
    def rerank(hits):
        return with_timeout(L2_POOL, lambda: serp_l2_scores(hits, session_id, user_id), SERP_L2_TIMEOUT_S)

    return (search(q, embed, query_index, mode, AI["serp_l1_k"], [SERP_INDEX_TEXT], rerank, SERP_N)
            | {"session_id": session_id, "user_id": user_id})


@app.get("/api/bench/qac_l1")
def bench_qac_l1(n: int = Query(100, ge=1, le=500)):
    """QAC L1 (Lakebase prefix lookup) latency from inside the app, back-to-back, cycling short and long prefixes."""
    prefixes = itertools.cycle(["a", "ca", "cam", "usb", "dog f", "iphone c", "nike shoes", "s", "lap", "wireless e"])
    return {"lookup": latency_stats(time_calls(lambda: prefix_rows(next(prefixes)), n))}


@app.get("/api/bench/qac_l1")
def bench_qac_l1(n: int = Query(100, ge=1, le=500)):
    """QAC L1 (Lakebase prefix lookup) latency from inside the app, back-to-back, cycling short and long prefixes."""
    prefixes = itertools.cycle(["a", "ca", "cam", "usb", "dog f", "iphone c", "nike shoes", "s", "lap", "wireless e"])
    return {"lookup": latency_stats(time_calls(lambda: prefix_rows(next(prefixes)), n))}


@app.get("/api/bench/qac_l2")
def bench_qac_l2(n: int = Query(100, ge=1, le=500), session_id: str | None = None, user_id: str | None = None,
                 gap_ms: float = Query(0, ge=0, le=2000)):
    """QAC L2 call latency from inside the app, no timeout: 1 vs `candidates` real L1 rows for "ca". Back-to-back by
    default; `gap_ms` idles before each call, like typing pace."""
    rows = prefix_rows("ca")
    sizes = sorted({1, min(QAC_L2["candidates"], len(rows))})
    return {f"candidates_{k}": latency_stats(time_calls(lambda: qac_l2_scores(rows[:k], session_id, user_id), n,
                                                         gap_ms=gap_ms))
            for k in sizes} | {"session_id": session_id, "user_id": user_id, "gap_ms": gap_ms}


@app.get("/api/scenarios")
def list_scenarios():
    return [{k: s[k] for k in ("scenario_id", "label", "user_id", "next_query")} for s in scenarios().values()]


@app.post("/api/scenarios/{scenario_id}/play")
def play(scenario_id: str):
    scenario = scenarios().get(scenario_id)
    if scenario is None:
        raise HTTPException(404, f"no scenario {scenario_id}")
    session_id = f"demo-{uuid.uuid4().hex[:10]}"
    sent, kafka_ack_ms = play_scenario(scenario, session_id, producer(), CFG["kafka"]["topic"],
                                       datetime.now(timezone.utc))
    PLAYED.record(session_id, {"expected": expected_session_features(sent), "last_event_time": sent[-1]["event_time"],
                               "kafka_ack_ms": kafka_ack_ms})
    return {"session_id": session_id, "user_id": scenario["user_id"], "events": len(sent), "kafka_ack_ms": kafka_ack_ms,
            "next_query": scenario["next_query"], "timeline": scenario_timeline(sent),
            "intent": {k: scenario[f"intent_{k}"] for k in ("category", "brand", "price_target")}}


def _utc(row: dict) -> dict:
    return {k: v.replace(tzinfo=timezone.utc) if isinstance(v, datetime) and v.tzinfo is None else v
            for k, v in row.items()}


@app.get("/api/sessions/{session_id}/freshness")
def freshness(session_id: str):
    """Waits until every session table reflects the replayed events, reading them every FRESHNESS_POLL_S, then
    returns the stage and per-feature breakdown. The poll interval bounds only the "app read it" stage."""
    played = PLAYED.get(session_id)
    if played is None:
        raise HTTPException(404, f"session {session_id} was not replayed by this app process")
    tables, seen = session_tables(), {}
    deadline = time.monotonic() + FRESHNESS_GIVE_UP_S
    with features_pool.connection() as conn:
        while len(seen) < len(tables) and time.monotonic() < deadline:
            for kind, name in tables.items():
                if kind in seen:
                    continue
                rows = [_utc(r) for r in conn.execute(f"SELECT * FROM {name} WHERE session_id = %s", (session_id,))]
                if landed(kind, played["expected"], rows, played["last_event_time"]):
                    seen[kind] = {"rows": rows, "detected_at": datetime.now(timezone.utc)}
            time.sleep(FRESHNESS_POLL_S)
    if len(seen) < len(tables):
        return {"status": "timeout", "landed": sorted(seen), "waited_s": FRESHNESS_GIVE_UP_S}
    return {"status": "ready"} | freshness_breakdown(seen, played["last_event_time"], played["kafka_ack_ms"])


@app.get("/api/users/{user_id}/features")
def user_features(user_id: str):
    """The user's batch (hourly) feature values as the online store holds them, read the way the L2 endpoint looks
    them up; for display, so read once per Play rather than per keystroke."""
    with features_pool.connection() as conn:
        rows = [[_utc(r) for r in conn.execute(f"SELECT * FROM {name} WHERE user_id = %s", (user_id,))]
                for name in user_tables().values()]
    return {"user_id": user_id, "features": user_feature_values(rows)}


_dist = ROOT / "app" / "frontend" / "dist"
if _dist.exists():
    app.mount("/", StaticFiles(directory=_dist, html=True), name="ui")
