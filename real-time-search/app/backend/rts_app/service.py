"""Request logic, kept free of Lakebase and Kafka clients so it is tested with fakes (main.py wires the real ones)."""

from __future__ import annotations

import json
import math
import time
from concurrent.futures import Executor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime
from typing import Callable

from rts_shared.events import event_payload
from rts_shared.retrieval import filter_completions, lookup_prefix
from rts_shared.scenarios import replay


def qac(typed: str, prefix_rows: Callable[[str], list[dict]], max_len: int, n: int,
        rerank: Callable[[list[dict]], list[float]] | None = None, candidates: int = 20) -> dict:
    """L1 completions for what was typed, and the L2 column: L1's top `candidates` reranked by `rerank` (scores in
    row order). Without an `ok` status the L2 column carries no items and the UI shows the L1 order."""
    prefix = lookup_prefix(typed, max_len)
    t = time.perf_counter()
    rows = prefix_rows(prefix) if prefix.strip() else []
    if len(lookup_prefix(typed, len(typed))) > max_len:
        rows = filter_completions(rows, typed)
    t_l1 = time.perf_counter()
    timings = {"l1": round((t_l1 - t) * 1000, 2)}
    if rerank is None:
        l2 = {"status": "not_deployed"}
    elif not rows:
        l2 = {"status": "ok", "items": []}
    else:
        pool = rows[:candidates]
        try:
            scores = rerank(pool)
            order = sorted(range(len(pool)), key=lambda i: -scores[i])  # stable: ties keep the L1 order
            l2 = {"status": "ok", "items": [{**pool[i], "l1_rank": i + 1} for i in order[:n]]}  # UI rank badges
        except TimeoutError:
            l2 = {"status": "timeout"}
        except Exception:
            l2 = {"status": "error"}
        timings["l2"] = round((time.perf_counter() - t_l1) * 1000, 2)
    return {"prefix": prefix, "l1": rows[:n], "l2": l2, "timings_ms": timings}


def with_timeout(executor: Executor, fn: Callable[[], object], timeout_s: float):
    """`fn()`, or TimeoutError after `timeout_s`. A slow call keeps running on its worker; its result is dropped."""
    future = executor.submit(fn)
    try:
        return future.result(timeout=timeout_s)
    except FutureTimeout:
        future.cancel()
        raise TimeoutError from None


def time_calls(fn: Callable[[], object], n: int, warmup: int = 3) -> list[float]:
    """Wall time in ms of `n` back-to-back `fn()` calls, after `warmup` untimed ones."""
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(n):
        t = time.perf_counter()
        fn()
        times.append((time.perf_counter() - t) * 1000)
    return times


def latency_stats(times_ms: list[float]) -> dict:
    """Nearest-rank percentiles, rounded to 0.1 ms."""
    xs = sorted(times_ms)
    pct = lambda q: round(xs[max(0, math.ceil(q * len(xs)) - 1)], 1)
    return {"n": len(xs), "min": round(xs[0], 1), "p50": pct(0.5), "p95": pct(0.95), "p99": pct(0.99),
            "max": round(xs[-1], 1)}


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
    (pay-per-token), and `embedding` says so. The L2 column carries only its status until SERP L2 exists."""
    text = q.strip()
    if not text:
        return {"query": text, "query_type": query_type, "embedding": "skipped", "l1": [],
                "l2": {"status": "not_deployed"}, "timings_ms": {}}
    t = time.perf_counter()
    try:
        vector, embedding = embed(text), "custom"
    except Exception:
        vector, embedding = None, "fallback_managed"
    t_embed = time.perf_counter()
    hits = query_index(**index_query(text, vector, query_type, k, query_columns))
    t_search = time.perf_counter()
    return {"query": text, "query_type": query_type, "embedding": embedding, "l1": hits,
            "l2": {"status": "not_deployed"},
            "timings_ms": {"embed": round((t_embed - t) * 1000, 2), "search": round((t_search - t_embed) * 1000, 2)}}


def play_scenario(scenario: dict, session_id: str, producer, topic: str, now: datetime) -> list[dict]:
    """Produce a scenario's events to Kafka as a new session; returns the events sent."""
    events = replay(json.loads(scenario["events"]), session_id, scenario["user_id"], now)
    for e in events:
        key, value = event_payload(e)
        producer.produce(topic, key=key, value=value)
    if producer.flush(10):
        raise RuntimeError("Kafka producer did not deliver every event within 10 s")
    return events


def scenario_timeline(events: list[dict]) -> list[dict]:
    """Replayed events for the UI, timed in seconds before the last one (as the session features saw them)."""
    last = events[-1]["event_time"]
    return [{"t_s": round((e["event_time"] - last).total_seconds(), 1),
             **{f: e[f] for f in ("event_type", "query", "product_id", "category", "brand", "price")}} for e in events]
