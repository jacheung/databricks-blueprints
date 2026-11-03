"""Request logic, kept free of Lakebase and Kafka clients so it is tested with fakes (main.py wires the real ones)."""

from __future__ import annotations

import json
import time
from datetime import datetime
from typing import Callable

from rts_shared.events import event_payload
from rts_shared.retrieval import filter_completions, lookup_prefix
from rts_shared.scenarios import replay


def qac(typed: str, prefix_rows: Callable[[str], list[dict]], max_len: int, n: int) -> dict:
    """L1 completions for what was typed, and the L2 column: the L1 order until a QAC L2 endpoint exists."""
    prefix = lookup_prefix(typed, max_len)
    t = time.perf_counter()
    rows = prefix_rows(prefix) if prefix.strip() else []
    if len(lookup_prefix(typed, len(typed))) > max_len:
        rows = filter_completions(rows, typed)
    l1 = rows[:n]
    return {"prefix": prefix, "l1": l1, "l2": {"status": "not_deployed", "items": l1},
            "timings_ms": {"l1": round((time.perf_counter() - t) * 1000, 2)}}


def index_query(text: str, vector: list[float] | None, query_type: str, k: int, query_columns: list[str]) -> dict:
    """`query_index` arguments. HYBRID needs the text for its keyword half; our vector, when given, replaces the
    index's managed (pay-per-token) embedding for the ANN half. ANN takes only one of the two."""
    kw = {"query_type": query_type, "num_results": k}
    if vector is not None:
        kw["query_vector"] = vector
    if query_type == "HYBRID" or vector is None:
        kw["query_text"] = text
    if query_type == "HYBRID" and query_columns:
        kw["query_columns"] = query_columns
    return kw


def rows_to_dicts(columns: list[str], data_array: list[list] | None) -> list[dict]:
    return [dict(zip(columns, row)) for row in data_array or []]


def search(q: str, embed: Callable[[str], list[float]], query_index: Callable[..., list[dict]], query_type: str,
           k: int, query_columns: list[str]) -> dict:
    """SERP L1: embed on our endpoint, then query the index. If our endpoint fails, the index embeds the text itself
    (pay-per-token), and `embedding` says so. The L2 column is the L1 order until a SERP L2 endpoint exists."""
    text = q.strip()
    if not text:
        return {"query": text, "query_type": query_type, "embedding": "skipped", "l1": [],
                "l2": {"status": "not_deployed", "items": []}, "timings_ms": {}}
    t = time.perf_counter()
    try:
        vector, embedding = embed(text), "custom"
    except Exception:
        vector, embedding = None, "fallback_managed"
    t_embed = time.perf_counter()
    hits = query_index(**index_query(text, vector, query_type, k, query_columns))
    t_search = time.perf_counter()
    return {"query": text, "query_type": query_type, "embedding": embedding, "l1": hits,
            "l2": {"status": "not_deployed", "items": hits},
            "timings_ms": {"embed": round((t_embed - t) * 1000, 2), "search": round((t_search - t_embed) * 1000, 2)}}


def play_scenario(scenario: dict, session_id: str, producer, topic: str, now: datetime) -> int:
    """Produce a scenario's events to Kafka as a new session; returns how many were sent."""
    events = replay(json.loads(scenario["events"]), session_id, scenario["user_id"], now)
    for e in events:
        key, value = event_payload(e)
        producer.produce(topic, key=key, value=value)
    if producer.flush(10):
        raise RuntimeError("Kafka producer did not deliver every event within 10 s")
    return len(events)
