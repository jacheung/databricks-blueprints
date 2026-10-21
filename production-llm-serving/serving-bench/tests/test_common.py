import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import (endpoint_label, incompatibility_message, parse_response, readout, readout_document, readout_markdown,
                    request_payload, requests_for_level)  # noqa: E402


def test_embedding_payloads():
    assert request_payload("pt", "embedding", "hi") == {"input": ["hi"]}
    assert request_payload("ppt", "embedding", "hi") == {"input": ["hi"]}
    assert request_payload("custom", "embedding", "hi") == {"dataframe_records": [{"text": "hi"}]}


def test_chat_payload_is_the_same_for_every_method():
    for method in ("pt", "ppt", "custom"):
        assert request_payload(method, "chat", "hi", max_tokens=64) == {
            "messages": [{"role": "user", "content": "hi"}],
            "max_tokens": 64,
            "temperature": 0.0,
            "stream": False,
        }


def test_parse_embedding_responses():
    assert parse_response("embedding", {"data": [{"embedding": [0.1, 0.2]}]}) == {"dim": 2}
    assert parse_response("embedding", {"predictions": [[0.1, 0.2, 0.3]]}) == {"dim": 3}
    with pytest.raises(ValueError):
        parse_response("embedding", {"predictions": [[]]})


def test_parse_chat_response():
    body = {
        "choices": [{"message": {"content": "hello"}}],
        "usage": {"prompt_tokens": 5, "completion_tokens": 2},
    }
    assert parse_response("chat", body) == {"prompt_tokens": 5, "completion_tokens": 2}
    with pytest.raises(ValueError):
        parse_response("chat", {"choices": []})


def test_endpoint_label():
    assert endpoint_label({"name": "x", "method": "pt"}) == "PT"
    assert endpoint_label({"name": "x", "method": "ppt"}) == "Pay-per-token"
    assert endpoint_label({"name": "x", "method": "custom", "workload_type": "GPU_SMALL"}) == (
        "Custom GPU (GPU_SMALL)"
    )
    assert endpoint_label({"name": "x", "method": "pt", "label": "Mine"}) == "Mine"


def test_readout_finds_saturation_and_idle_delta():
    summary = [
        {"endpoint": "A", "concurrency": 1, "rps": 10, "p50": 50, "p95": 60, "errors": 0},
        {"endpoint": "A", "concurrency": 2, "rps": 30, "p50": 60, "p95": 70, "errors": 0},
        {"endpoint": "A", "concurrency": 4, "rps": 31, "p50": 120, "p95": 150, "errors": 1},
        {"endpoint": "B", "concurrency": 1, "rps": 5, "p50": 80, "p95": 90, "errors": 0},
    ]
    idle = [
        {"endpoint": "A", "idle_seconds": 0, "latency_ms": 100},
        {"endpoint": "A", "idle_seconds": 0, "latency_ms": 120},
        {"endpoint": "A", "idle_seconds": 300, "latency_ms": 150},
        {"endpoint": "A", "idle_seconds": 300, "latency_ms": None},
    ]
    rows = {r["endpoint"]: r for r in readout(summary, idle)}
    assert rows["A"] == {
        "endpoint": "A",
        "peak_rps": 31,
        "saturation_concurrency": 2,
        "p50_at_saturation_ms": 60,
        "p95_at_saturation_ms": 70,
        "errors": 1,
        "warm_ms": 110,
        "longest_idle_s": 300,
        "after_idle_ms": 150,
        "after_idle_std_ms": None,
        "idle_failures": 1,
        "note": None,
    }
    assert rows["B"]["saturation_concurrency"] == 1
    assert rows["B"]["after_idle_ms"] is None


def test_incompatibility_message_names_versions_and_next_step():
    model = {"hf_name": "Alibaba-NLP/gte-large-en-v1.5", "ai_env_candidates": ["6", "5", "4"]}
    versions = {"ai_env": "6", "transformers": "5.8.1", "torch": "2.9", "cuda": "12.8"}
    message = incompatibility_message(model, versions, IndexError("index out of range"))
    assert "AI env v6" in message and "transformers 5.8.1" in message
    assert "IndexError: index out of range" in message
    assert "00_compat_check" in message and "6, 5, 4" in message


def test_requests_scale_with_concurrency():
    load_test = {"requests_per_level": 100, "requests_per_client": 20}
    assert requests_for_level(load_test, 1) == 100
    assert requests_for_level(load_test, 8) == 160
    assert requests_for_level(load_test, 32) == 640
    assert requests_for_level({"requests_per_level": 100}, 32) == 100


def test_readout_reports_idle_std():
    summary = [{"endpoint": "A", "concurrency": 1, "rps": 10, "p50": 50, "p95": 60, "errors": 0}]
    idle = [{"endpoint": "A", "idle_seconds": 300, "latency_ms": v} for v in (100, 120, 140)]
    (row,) = readout(summary, idle)
    assert row["after_idle_ms"] == 120
    assert row["after_idle_std_ms"] == 20


def test_readout_document_records_run_and_settings():
    rows = readout(
        [{"endpoint": "PT", "concurrency": 1, "rps": 10, "p50": 50, "p95": 60, "errors": 0}], []
    )
    doc = readout_document(
        {"run_id": "run_x", "model": "owner/model", "task": "embedding", "input_tokens": 256,
         "date": "2026-10-21", "workspace": "https://example.cloud.databricks.com",
         "ai_env": "5",
         "endpoints": [
             {"name": "sb-pt", "method": "pt", "label": "PT", "entity_name": "system.ai.m",
              "entity_version": "2", "min_provisioned_throughput": 19000, "max_provisioned_throughput": 19000},
             {"name": "sb-t4", "method": "custom", "label": "Custom GPU (T4)", "workload_type": "GPU_SMALL",
              "scale_to_zero_enabled": False},
         ],
         "load_test": {"concurrency_levels": [1, 2], "requests_per_level": 100, "requests_per_client": 20},
         "idle_test": {"idle_seconds": [5, 300], "reps": 3}},
        rows,
    )
    assert doc.startswith("# owner/model")
    assert "run_x" in doc and "2026-10-21" in doc and "256" in doc
    assert "`sb-pt`" in doc
    assert "system.ai.m v2, 19000–19000 tokens/s" in doc
    assert "GPU_SMALL, scale-to-zero off" in doc
    assert "AI env v5" in doc
    assert "| PT | 10.0 |" in doc
    assert "Std after longest idle" in doc


def test_readout_excludes_error_heavy_levels_and_flags_rate_limits():
    summary = [
        {"endpoint": "PPT", "concurrency": 8, "requests": 160, "rps": 48, "p50": 68, "p95": 195, "errors": 0},
        {"endpoint": "PPT", "concurrency": 16, "requests": 320, "rps": 155, "p50": 68, "p95": 168, "errors": 0},
        {"endpoint": "PPT", "concurrency": 32, "requests": 640, "rps": 243, "p50": 72, "p95": 212,
         "errors": 332, "rate_limited": 332},
    ]
    (row,) = readout(summary, [])
    assert row["peak_rps"] == 155
    assert row["saturation_concurrency"] == 16
    assert row["errors"] == 332
    assert row["note"] == "C32 excluded: 52% errors (rate-limited, HTTP 429)"
    assert "C32 excluded" in readout_markdown([row])


def test_readout_tolerates_rare_errors_and_handles_all_bad_levels():
    ok = [{"endpoint": "A", "concurrency": 1, "requests": 1000, "rps": 10, "p50": 5, "p95": 6, "errors": 5}]
    (row,) = readout(ok, [])
    assert row["peak_rps"] == 10 and row["note"] is None
    bad = [{"endpoint": "B", "concurrency": 1, "requests": 100, "rps": 1, "p50": 5, "p95": 6, "errors": 50}]
    (row,) = readout(bad, [])
    assert row["peak_rps"] is None and row["saturation_concurrency"] is None
    assert row["note"] == "C1 excluded: 50% errors"
    assert "| B | — |" in readout_markdown([row])
