import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from make_readout import CHARTS, level_table, normalize, series_colors, write_readout  # noqa: E402

META = {
    "run_id": "run_x", "model": "owner/model", "task": "embedding", "input_tokens": 256,
    "date": "2026-10-21", "workspace": "https://example.cloud.databricks.com", "ai_env": "5",
    "endpoints": [
        {"name": "sb-pt", "method": "pt", "label": "PT", "entity_name": "system.ai.m", "entity_version": "2",
         "min_provisioned_throughput": 19000, "max_provisioned_throughput": 19000},
        {"name": "ppt", "method": "ppt", "label": "Pay-per-token"},
    ],
    "load_test": {"concurrency_levels": [1, 2], "requests_per_level": 100, "requests_per_client": 20},
    "idle_test": {"idle_seconds": [5, 300], "reps": 2},
}
SUMMARY = [
    {"endpoint": "PT", "concurrency": 1, "requests": 100, "rps": 15.9, "p50": 58.0, "p95": 110.0,
     "p99": 115.0, "errors": 0, "rate_limited": 0, "duration_s": 6.3},
    {"endpoint": "PT", "concurrency": 2, "requests": 100, "rps": 34.3, "p50": 56.0, "p95": 68.0,
     "p99": 76.0, "errors": 0, "rate_limited": 0, "duration_s": 2.9},
    {"endpoint": "Pay-per-token", "concurrency": 1, "requests": 100, "rps": 3.7, "p50": 80.0, "p95": 853.0,
     "p99": 900.0, "errors": 0, "rate_limited": 0, "duration_s": 27.0},
    {"endpoint": "Pay-per-token", "concurrency": 2, "requests": 100, "rps": 9.0, "p50": 67.0, "p95": 221.0,
     "p99": 300.0, "errors": 40, "rate_limited": 40, "duration_s": 9.2},
]
REQUESTS = (
    [{"endpoint": e, "phase": "load", "concurrency": c, "idle_seconds": None, "latency_ms": 50.0 + i,
      "success": True} for e in ("PT", "Pay-per-token") for c in (1, 2) for i in range(20)]
    + [{"endpoint": e, "phase": "idle_probe", "concurrency": None, "idle_seconds": s, "latency_ms": 100.0 + r,
        "success": True} for e in ("PT", "Pay-per-token") for s in (0, 5, 300) for r in range(2)]
)


def test_normalize_converts_cli_strings():
    row = normalize({"concurrency": "32", "idle_seconds": "", "latency_ms": "22.8", "success": "false",
                     "status": "429", "endpoint": "PPT"})
    assert row == {"concurrency": 32, "idle_seconds": None, "latency_ms": 22.8, "success": False,
                   "status": 429, "endpoint": "PPT"}


def test_level_table_marks_excluded_levels():
    table = level_table(SUMMARY)
    assert "| PT | 1 | 100 | 15.9 | 58 | 110 | 115 | 0 | 6.3 |  |" in table
    assert "| Pay-per-token | 2 | 100 | 9.0 | 67 | 221 | 300 | 40 | 9.2 | excluded: 40% errors (429) |" in table


def test_write_readout_writes_markdown_and_every_chart(tmp_path):
    out = write_readout(tmp_path / "owner-model", META, SUMMARY, REQUESTS)
    doc = (out / "README.md").read_text()
    assert doc.startswith("# owner/model")
    for name in CHARTS:
        assert (out / name).stat().st_size > 1000
        assert f"]({name})" in doc
    assert "## Per-level results" in doc


def test_series_colors_follow_the_option_not_its_position():
    gte = series_colors(["PT", "Custom GPU (T4)", "Custom GPU (A10G)", "Pay-per-token"])
    minilm = series_colors(["Custom GPU (T4)", "Custom GPU (A10G)"])
    assert minilm["Custom GPU (T4)"] == gte["Custom GPU (T4)"]
    assert minilm["Custom GPU (A10G)"] == gte["Custom GPU (A10G)"]
    custom = series_colors(["PT", "My endpoint"])
    assert custom["My endpoint"] not in {custom["PT"]}
