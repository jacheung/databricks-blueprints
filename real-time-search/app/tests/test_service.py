import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest

from rts_app.service import (index_query, latency_stats, play_scenario, qac, rows_to_dicts, scenario_timeline, search,
                             time_calls, with_timeout)
from rts_shared.events import EVENT_FIELDS

TABLE = {
    "ca": [{"rank": 1, "completion": "camera"}, {"rank": 2, "completion": "camera bag"}, {"rank": 3, "completion": "cat food"}],
    "camera bag f": [{"rank": 1, "completion": "camera bag for sony"}, {"rank": 2, "completion": "camera bag for canon"}],
}


def lookup(prefix):
    return TABLE.get(prefix, [])


def test_qac_returns_l1_and_falls_back_to_it_while_l2_is_not_deployed():
    out = qac("Ca", lookup, max_len=12, n=2)
    assert [c["completion"] for c in out["l1"]] == ["camera", "camera bag"]
    assert out["l2"] == {"status": "not_deployed"}  # the UI shows the L1 order; no duplicate list
    assert out["prefix"] == "ca" and out["timings_ms"]["l1"] >= 0


def test_qac_filters_when_typed_text_is_longer_than_the_key():
    out = qac("camera bag for s", lookup, max_len=12, n=10)
    assert [c["completion"] for c in out["l1"]] == ["camera bag for sony"]


def test_qac_skips_lookup_for_blank_input():
    calls = []
    out = qac("   ", lambda p: calls.append(p) or [], max_len=12, n=10)
    assert out["l1"] == [] and calls == []


class FakeProducer:
    def __init__(self):
        self.sent, self.flushed = [], False

    def produce(self, topic, key, value):
        self.sent.append((topic, key, json.loads(value)))

    def flush(self, timeout=None):
        self.flushed = True
        return 0


SCENARIO = {"scenario_id": "sc1", "user_id": "u42", "events": json.dumps([
    {"offset_ms": 0, "search_id": "s9-0", "event_type": "search", "query_id": 7, "query": "canon lens",
     "product_id": None, "category": "Camera & Photo", "brand": None, "price": None, "device": "desktop"},
    {"offset_ms": 900, "search_id": "s9-0", "event_type": "click", "query_id": 7, "query": "canon lens",
     "product_id": "B01", "category": "Camera & Photo", "brand": "canon", "price": 450.0, "device": "desktop"}])}


def test_play_scenario_produces_every_event_under_the_new_session_and_flushes():
    p = FakeProducer()
    sent = play_scenario(SCENARIO, "demo-abc", p, "topic1", now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert len(sent) == 2 and p.flushed
    assert [(t, k) for t, k, _ in p.sent] == [("topic1", b"demo-abc")] * 2
    assert all(set(v) == {f for f, _ in EVENT_FIELDS} and v["session_id"] == "demo-abc" for _, _, v in p.sent)
    assert p.sent[1][2]["user_id"] == "u42" and p.sent[1][2]["event_type"] == "click"


def test_scenario_timeline_shows_what_was_replayed_relative_to_the_last_event():
    sent = play_scenario(SCENARIO, "demo-abc", FakeProducer(), "topic1", now=datetime(2026, 11, 2, tzinfo=timezone.utc))
    assert scenario_timeline(sent) == [
        {"t_s": -0.9, "event_type": "search", "query": "canon lens", "product_id": None, "category": "Camera & Photo",
         "brand": None, "price": None},
        {"t_s": 0.0, "event_type": "click", "query": "canon lens", "product_id": "B01", "category": "Camera & Photo",
         "brand": "canon", "price": 450.0}]


VEC = [0.1, 0.2]
HITS = [{"product_id": "B1", "title": "usb c cable", "score": 0.9}, {"product_id": "B2", "title": "usb hub", "score": 0.8}]


def test_hybrid_query_sends_text_for_keywords_and_our_vector_for_ann():
    kw = index_query("usb c", VEC, "HYBRID", k=200, query_columns=["search_text"])
    assert kw == {"query_text": "usb c", "query_vector": VEC, "query_type": "HYBRID", "num_results": 200,
                  "query_columns": ["search_text"]}


def test_ann_query_sends_only_the_vector():
    # The index rejects ANN requests with both: "Only one of Field 'query_text' or Field 'query_vector'".
    kw = index_query("usb c", VEC, "ANN", k=200, query_columns=["search_text"])
    assert kw == {"query_vector": VEC, "query_type": "ANN", "num_results": 200}


def test_without_a_vector_the_index_embeds_the_text_itself():
    assert index_query("usb c", None, "ANN", k=50, query_columns=[]) == {
        "query_text": "usb c", "query_type": "ANN", "num_results": 50}
    assert "query_vector" not in index_query("usb c", None, "HYBRID", k=50, query_columns=["search_text"])


def test_rows_to_dicts_names_columns_from_the_manifest():
    assert rows_to_dicts(["product_id", "title", "score"], [["B1", "usb c cable", 0.9]]) == [HITS[0]]
    assert rows_to_dicts(["product_id"], None) == []


def test_search_embeds_on_our_endpoint_then_queries_the_index():
    calls = []
    out = search(" USB C ", embed=lambda t: calls.append(("embed", t)) or VEC,
                 query_index=lambda **kw: calls.append(("index", kw)) or HITS, query_type="HYBRID", k=200,
                 query_columns=["search_text"])
    assert calls[0] == ("embed", "USB C") and calls[1][1]["query_vector"] == VEC
    assert out["l1"] == HITS and out["l2"] == {"status": "not_deployed"}
    assert out["embedding"] == "custom" and out["query_type"] == "HYBRID"
    assert set(out["timings_ms"]) == {"embed", "search"}


def test_search_falls_back_to_managed_embedding_when_our_endpoint_fails():
    sent = {}

    def broken(_):
        raise TimeoutError("rts-query-embedding")

    out = search("usb c", embed=broken, query_index=lambda **kw: sent.update(kw) or HITS, query_type="HYBRID",
                 k=200, query_columns=["search_text"])
    assert "query_vector" not in sent and sent["query_text"] == "usb c"
    assert out["embedding"] == "fallback_managed" and out["l1"] == HITS


def test_search_skips_both_calls_for_blank_input():
    out = search("  ", embed=lambda t: pytest.fail("embedded"), query_index=lambda **kw: pytest.fail("searched"),
                 query_type="HYBRID", k=200, query_columns=[])
    assert out["l1"] == [] and out["embedding"] == "skipped"


RANKED = {"ca": [{"rank": i, "completion": c} for i, c in enumerate(["camera", "camera bag", "cat food", "car seat"], 1)]}


def test_qac_l2_reranks_the_top_candidates_and_shows_the_top_n():
    sent = []
    scores = {"camera": 0.1, "camera bag": 0.9, "cat food": 0.5}
    out = qac("ca", lambda p: RANKED.get(p, []), max_len=12, n=2, candidates=3,
              rerank=lambda rows: sent.append(rows) or [scores[r["completion"]] for r in rows])
    assert [r["completion"] for r in sent[0]] == ["camera", "camera bag", "cat food"]  # L1 top 3 only
    assert out["l2"]["status"] == "ok"
    assert [c["completion"] for c in out["l2"]["items"]] == ["camera bag", "cat food"]
    assert [c["completion"] for c in out["l1"]] == ["camera", "camera bag"]
    assert out["timings_ms"]["l2"] >= 0


def test_qac_l2_items_carry_their_l1_position_for_the_ui_badges():
    out = qac("ca", lambda p: RANKED.get(p, []), max_len=12, n=3, candidates=4,
              rerank=lambda rows: [0.1, 0.2, 0.3, 0.9])
    assert [(c["completion"], c["l1_rank"]) for c in out["l2"]["items"]] == [("car seat", 4), ("cat food", 3),
                                                                            ("camera bag", 2)]
    assert all("l1_rank" not in c for c in out["l1"])  # L1 rows are not mutated


def test_qac_l1_rank_is_the_position_after_filtering_not_the_table_rank():
    out = qac("camera bag for c", lookup, max_len=12, n=10, rerank=lambda rows: [1.0] * len(rows))
    assert [(c["completion"], c["rank"], c["l1_rank"]) for c in out["l2"]["items"]] == [("camera bag for canon", 2, 1)]


def test_qac_l2_timeout_and_error_fall_back_to_l1_without_items():
    def slow(rows):
        raise TimeoutError

    def broken(rows):
        raise RuntimeError("endpoint 503")

    for rerank, status in [(slow, "timeout"), (broken, "error")]:
        out = qac("ca", lambda p: RANKED.get(p, []), max_len=12, n=2, candidates=3, rerank=rerank)
        assert out["l2"] == {"status": status} and len(out["l1"]) == 2
        assert "l2" in out["timings_ms"]


def test_qac_l2_is_not_called_without_candidates():
    out = qac("zz", lambda p: [], max_len=12, n=2, candidates=3, rerank=lambda rows: pytest.fail("called"))
    assert out["l2"] == {"status": "ok", "items": []}


def test_with_timeout_returns_fast_results_and_raises_on_slow_ones():
    pool = ThreadPoolExecutor(max_workers=2)
    assert with_timeout(pool, lambda: [1.0], timeout_s=1) == [1.0]
    with pytest.raises(TimeoutError):
        with_timeout(pool, lambda: time.sleep(0.2), timeout_s=0.01)


def test_latency_stats_reports_nearest_rank_percentiles():
    stats = latency_stats([float(x) for x in range(1, 101)])
    assert stats == {"n": 100, "min": 1.0, "p50": 50.0, "p95": 95.0, "p99": 99.0, "max": 100.0}


def test_time_calls_runs_n_times_after_warmup_and_returns_ms():
    calls = []
    times = time_calls(lambda: calls.append(1), n=5, warmup=2)
    assert len(calls) == 7 and len(times) == 5 and all(t >= 0 for t in times)
