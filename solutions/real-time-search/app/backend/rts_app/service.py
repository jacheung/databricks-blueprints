"""Request logic, kept free of Lakebase and Kafka clients so it is tested with fakes (main.py wires the real ones)."""

from __future__ import annotations

import json
import math
import threading
import time
from collections import OrderedDict
from concurrent.futures import Executor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime, timezone
from typing import Callable

from rts_shared.events import event_payload
from rts_shared.features import feature_keys
from rts_shared.retrieval import QAC_L2_REQUEST_COLUMNS, filter_completions, lookup_prefix
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


def time_calls(fn: Callable[[], object], n: int, warmup: int = 3, gap_ms: float = 0) -> list[float]:
    """Wall time in ms of `n` `fn()` calls, after `warmup` untimed ones: back-to-back, or idle for `gap_ms` (untimed)
    before each, like keystrokes at typing pace."""
    for _ in range(warmup):
        fn()
    times = []
    for _ in range(n):
        time.sleep(gap_ms / 1000)
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
           k: int, query_columns: list[str], rerank: Callable[[list[dict]], list[float]] | None = None,
           n: int | None = None) -> dict:
    """SERP L1: embed on our endpoint, then query the index. If our endpoint fails, the index embeds the text itself
    (pay-per-token), and `embedding` says so. L2 reranks every hit (scores in hit order), as `qac` does; both
    columns show their top `n`. Without an `ok` status the L2 column carries no items and the UI shows the L1 order."""
    text = q.strip()
    if not text:
        return {"query": text, "query_type": query_type, "embedding": "skipped", "hits": 0, "l1": [],
                "l2": {"status": "not_deployed"}, "timings_ms": {}}
    t = time.perf_counter()
    try:
        vector, embedding = embed(text), "custom"
    except Exception:
        vector, embedding = None, "fallback_managed"
    t_embed = time.perf_counter()
    hits = query_index(**index_query(text, vector, query_type, k, query_columns))
    t_search = time.perf_counter()
    timings = {"embed": round((t_embed - t) * 1000, 2), "search": round((t_search - t_embed) * 1000, 2)}
    if rerank is None:
        l2 = {"status": "not_deployed"}
    elif not hits:
        l2 = {"status": "ok", "items": []}
    else:
        try:
            scores = rerank(hits)
            order = sorted(range(len(hits)), key=lambda i: -scores[i])  # stable: ties keep the L1 order
            l2 = {"status": "ok", "items": [{**hits[i], "l1_rank": i + 1} for i in order[:n]]}  # UI rank badges
        except TimeoutError:
            l2 = {"status": "timeout"}
        except Exception:
            l2 = {"status": "error"}
        timings["l2"] = round((time.perf_counter() - t_search) * 1000, 2)
    return {"query": text, "query_type": query_type, "embedding": embedding, "hits": len(hits), "l1": hits[:n],
            "l2": l2, "timings_ms": timings}


def play_scenario(scenario: dict, session_id: str, producer, topic: str, now: datetime,
                  clock: Callable[[], datetime] | None = None) -> tuple[list[dict], float]:
    """Produce a scenario's events to Kafka as a new session; returns the events sent and the last one's Kafka write
    in ms. The replay ends on its last click or cart, the only event that moves all three session tables, so every
    table's timestamps time that event. The rest is sent and flushed first, untimed; the last event is then stamped
    at send time (`clock`) and sent alone, so the Kafka write is one event's, not the whole replay's."""
    events = replay(json.loads(scenario["events"]), session_id, scenario["user_id"], now)
    engaged = [i for i, e in enumerate(events) if e["event_type"] in ENGAGED]
    if engaged:
        events = events[:engaged[-1] + 1]
    for e in events[:-1]:
        key, value = event_payload(e)
        producer.produce(topic, key=key, value=value)
    if producer.flush(10):
        raise RuntimeError("Kafka producer did not deliver every event within 10 s")
    events[-1] = {**events[-1], "event_time": (clock or (lambda: datetime.now(timezone.utc)))()}
    key, value = event_payload(events[-1])
    t = time.perf_counter()
    producer.produce(topic, key=key, value=value)
    if producer.flush(10):
        raise RuntimeError("Kafka producer did not deliver the last event within 10 s")
    return events, round((time.perf_counter() - t) * 1000, 1)


def scenario_timeline(events: list[dict]) -> list[dict]:
    """Replayed events for the UI, timed in seconds before the last one (as the session features saw them)."""
    last = events[-1]["event_time"]
    return [{"t_s": round((e["event_time"] - last).total_seconds(), 1),
             **{f: e[f] for f in ("event_type", "query", "product_id", "category", "brand", "price")}} for e in events]


ENGAGED = ("click", "add_to_cart")
# The session Feature Views' online tables, each named by a feature only it holds (data/src/rts_data/feature_specs.py).
def l2_inputs(cfg: dict) -> dict[str, list[str]]:
    """What the QAC L2 endpoint receives, by source: session features from the Kafka stream, user features from the
    hourly Delta batch, and the candidate fields the app sends. Item features aren't QAC L2 inputs."""
    keys = feature_keys(cfg)
    return {"stream": [f for f, k in keys.items() if k[0] == "session_id"],
            "batch": [f for f, k in keys.items() if k[0] == "user_id"],
            "request": list(QAC_L2_REQUEST_COLUMNS)}


def user_feature_values(table_rows: list[list[dict]]) -> list[dict]:
    """A user's online feature values from each user table's rows, lookup keys dropped; the per-category count is
    one value per category, largest first."""
    out = []
    for rows in table_rows:
        for r in sorted(rows, key=lambda r: -(r.get("user_cat_cnt_30d") or 0)):
            out += [{"feature": f"{k}[{r['category']}]" if "category" in r else k, "value": v}
                    for k, v in r.items() if k not in ("user_id", "category")]
    return out


SESSION_TABLES = {"window": "sess_n_clicks_30m", "category": "sess_cat_cnt_30m", "last": "sess_last_event_type"}
WINDOW_FEATURES = ("sess_n_searches_30m", "sess_n_clicks_30m", "sess_n_carts_30m", "sess_avg_price_30m",
                   "sess_min_price_30m", "sess_max_price_30m", "sess_std_price_30m")
LAST_FEATURES = ("sess_last_event_type", "sess_last_brand", "sess_last_category", "sess_last_price")


def expected_session_features(events: list[dict]) -> dict:
    """What each session table holds once every replayed event has landed, as the online tables show it."""
    engaged = [e for e in events if e["event_type"] in ENGAGED]
    categories = {}
    for e in engaged:
        if e["category"]:
            categories[e["category"]] = categories.get(e["category"], 0) + 1
    last = engaged[-1] if engaged else {}
    return {"window": {"sess_n_searches_30m": sum(e["event_type"] == "search" for e in events),
                       "sess_n_clicks_30m": sum(e["event_type"] == "click" for e in events),
                       "sess_n_carts_30m": sum(e["event_type"] == "add_to_cart" for e in events)},
            "category": categories,
            # Observed online: every sess_last_* column, event type included, is the last click or cart, although
            # feature_specs defines sess_last_event_type over all events (2026-11-03; flagged to data/ml).
            "last": {"sess_last_event_type": last.get("event_type"), "sess_last_brand": last.get("brand"),
                     "sess_last_category": last.get("category"), "sess_last_price": last.get("price")}}


def landed(table: str, expected: dict, rows: list[dict], last_event_time: datetime) -> bool:
    """Whether a session table's rows reflect every replayed event and were written for the timed last event (a
    click or cart, so it moves every table): committed after it was stamped and, where the row records it, carrying
    its event_time. Otherwise the row's timestamps would time an earlier event."""
    def fresh(r):
        return (r["commit_time"] >= last_event_time
                and ("event_time" not in r or abs((r["event_time"] - last_event_time).total_seconds()) < 0.001))

    want = expected[table]
    if table == "category":
        current = [r for r in rows if r["category"] == expected["last"]["sess_last_category"]]
        return {r["category"]: r["sess_cat_cnt_30m"] for r in rows} == want and all(fresh(r) for r in current)
    if len(rows) != 1 or not fresh(rows[0]):
        return False
    row = rows[0]
    if table == "window":
        return all(row[k] == v for k, v in want.items())
    price = want["sess_last_price"]
    return (all(row[k] == want[k] for k in ("sess_last_event_type", "sess_last_brand", "sess_last_category"))
            and (price is None or abs((row["sess_last_price"] or 0) - price) < 1e-3))


def freshness_breakdown(seen: dict, last_event_time: datetime, kafka_ack_ms: float) -> dict:
    """Per-stage and per-feature freshness once every table has landed. `seen[table]` holds its rows and when the
    app first read them complete. Replayed events keep their simulated event_time but are all produced at Play, so
    every offset is from the last event, the only one whose event_time is the real produce time."""
    def since(t):
        return round((t - last_event_time).total_seconds() * 1000, 1)

    def newest(rows, col):
        return max(r[col] for r in rows)

    # The breakdown follows the click count's table; each stage ends at a timestamp, the last when the app had read
    # every table, so the stages add up to the total. App and pipeline clocks differ by a few ms, and the app's ack
    # can return after the pipeline already read the event, so a stage is clamped at 0 rather than going negative.
    w = seen["window"]["rows"]
    total = max(since(v["detected_at"]) for v in seen.values())
    points = [("Kafka write", "app", kafka_ack_ms),
              ("Real-Time Mode read + computed", "pipeline · output_time", since(newest(w, "output_time"))),
              ("Lakebase write", "pipeline · commit_time", since(newest(w, "commit_time"))),
              ("App read every feature", "app · Lakebase", total)]
    stages, prev = [], 0.0
    for name, source, at in points:
        stages.append({"name": name, "source": source, "ms": round(max(0.0, at - prev), 1)})
        prev = max(prev, at)
    features = []
    for table, names in (("window", WINDOW_FEATURES), ("last", LAST_FEATURES)):
        rows, at = seen[table]["rows"], seen[table]["detected_at"]
        features += [{"feature": n, "table": table, "value": rows[0].get(n),
                      "in_lakebase_ms": since(newest(rows, "commit_time")), "served_ms": since(at)} for n in names]
    at = seen["category"]["detected_at"]
    # A category the timed click didn't touch was committed with the untimed rest of the replay: no offset to show.
    features += [{"feature": f"sess_cat_cnt_30m[{r['category']}]", "table": "category", "value": r["sess_cat_cnt_30m"],
                  **({"in_lakebase_ms": since(r["commit_time"]), "served_ms": since(at)}
                     if r["commit_time"] >= last_event_time else {"in_lakebase_ms": None, "served_ms": None})}
                 for r in sorted(seen["category"]["rows"], key=lambda r: -r["sess_cat_cnt_30m"])]
    return {"total_ms": total, "stages": stages, "features": features}


class RecentValues:
    """Values by id that the UI reads after its request returned (full L2 call times, replayed sessions). Bounded:
    the oldest are forgotten."""

    def __init__(self, max_size: int = 1000):
        self._values, self._max, self._lock = OrderedDict(), max_size, threading.Lock()

    def record(self, key: str, value):
        with self._lock:
            self._values[key] = value
            while len(self._values) > self._max:
                self._values.popitem(last=False)

    def get(self, key: str):
        with self._lock:
            return self._values.get(key)


def ranked_top(items: list, scores: list[float], k: int) -> list:
    """The top `k` items by L2 score; ties keep the L1 order, as `qac` and `search` rank."""
    return [items[i] for i in sorted(range(len(items)), key=lambda i: -scores[i])[:k]]


def persona_lift(guest_tops: list[list[dict]], persona_tops: list[list[dict]], category: str, min_lift: float) -> dict:
    """Share of `category` among the top results (one list per prefix or query), as a guest and as the persona. `ok`
    when the persona's share is at least `min_lift` times the guest's, and above it."""
    def share(tops):
        return round(sum(r["category"] == category for t in tops for r in t) / max(1, sum(len(t) for t in tops)), 3)

    guest, persona = share(guest_tops), share(persona_tops)
    return {"guest": guest, "persona": persona, "ok": persona > guest and persona >= min_lift * guest}


def trending(rows: list[dict], n: int, max_words: int = 3) -> list[dict]:
    """Trending searches for the home page from QAC prefix rows (one-letter prefixes cover the popular completions):
    one per completion of at most `max_words` words (a chip), most searched in 7 days first, ties broken on 30 days."""
    best = {r["completion"]: r for r in rows if len(r["completion"].split()) <= max_words}
    top = sorted(best.values(), key=lambda r: (-r["pop_7d"], -r["pop_30d"]))[:n]
    return [{"query": r["completion"], "category": r["category"]} for r in top]
