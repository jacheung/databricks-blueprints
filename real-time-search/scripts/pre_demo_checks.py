"""Pre-demo checks: is everything the demo needs up, current and wired together? Run by `scripts/pre_demo.sh` after
it brings everything up; also runs on its own. From this folder's parent (`real-time-search/`):

    uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I scripts/pre_demo_checks.py \\
        shared/configs/dev.yml

Uses the CLI OAuth login for `workspace.profile`: route-optimized endpoints and the app accept only OAuth tokens.
Prints PASS/FAIL per check and exits 1 if any fails.
"""

from __future__ import annotations

import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path[:0] = [os.path.join(HERE, "..", p) for p in ("shared/src", "data/src", "ml/scripts")]
from rts_data.feature_specs import feature_specs  # noqa: E402
from rts_shared.config import full_name, load_config, window_strength  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

APP = "rts-search"            # app/resources/app.yml
MIN_STRENGTH = 0.95           # latest window vs the best day (time_shift asserts the same)
FRESHNESS_WAIT_S = 360        # the session pipeline needs ~4 min after a start to catch up on its backlog


def endpoints_ready(w, cfg):
    """READY and scale-to-zero as configured (off): the workspace's cleanup bot turns it on overnight."""
    states = {}
    for m in ("qac_l2", "query_embedding", "serp_l2"):
        e = w.serving_endpoints.get(cfg["models"][m]["endpoint"])
        states[e.name] = (str(e.state.ready).rsplit(".", 1)[-1],
                          [bool(s.scale_to_zero_enabled) for s in e.config.served_entities] ==
                          [cfg["models"][m]["scale_to_zero_enabled"]])
    return all(r == "READY" and scale_ok for r, scale_ok in states.values()), \
        {n: f"{r}, scale-to-zero {'as configured' if ok else 'WRONG'}" for n, (r, ok) in states.items()}


def vector_search_online(w, cfg):
    ai = cfg["ai_search"]
    state = str(w.vector_search_endpoints.get_endpoint(ai["endpoint"]).endpoint_status.state)
    index = w.vector_search_indexes.get_index(index_name=full_name(cfg, ai["index"])).status
    return state.endswith("ONLINE") and bool(index.ready), {"endpoint": state, "index_ready": index.ready,
                                                              "indexed_rows": index.indexed_row_count}


def query_embedding(w, cfg, config_path):
    from check_query_embedding import main  # ml/scripts; raises AssertionError on a failed check
    main(config_path)
    return True, "custom embeddings match pay-per-token"


def batch_features_fresh(w, cfg):
    """Latest window ends today (UTC) and holds >= MIN_STRENGTH of the best day: stale history fades it."""
    warehouse = next(iter(w.warehouses.list())).id  # any warehouse; serverless ones start on demand
    today, report = datetime.now(timezone.utc).date().isoformat(), {}
    for spec in feature_specs(cfg):
        if spec.group not in ("user", "item") or spec.op != "Count":
            continue
        (offline,) = [m.table_name for m in w.feature_engineering.list_materialized_features(
            feature_name=full_name(cfg, spec.name)) if not m.is_online]
        r = w.statement_execution.execute_statement(
            statement=f"SELECT DATE(event_time), SUM({spec.name}) FROM {offline} GROUP BY 1", warehouse_id=warehouse,
            wait_timeout="50s")
        report[spec.name] = window_strength([(datetime.fromisoformat(d).date(), float(s))
                                             for d, s in r.result.data_array])
    ok = all(v["latest_date"] == today and v["ratio"] >= MIN_STRENGTH for v in report.values())
    return ok, {k: (v["latest_date"], v["ratio"]) for k, v in report.items()}


def _app_call(w, path, method="GET", timeout=180):
    url = w.apps.get(APP).url.rstrip("/") + path
    req = urllib.request.Request(url, method=method, headers=w.config.authenticate())
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.load(resp)


def personas_lift(w, cfg):
    report = _app_call(w, "/api/checks/personas")
    return bool(report["ok"]), {p["name"]: (p["qac_top5"]["ok"], p["serp_top10"]["ok"]) for p in report["personas"]}


def session_freshness(w, cfg):
    """Plays one scenario and waits for its events to land in every session table (each call waits up to 15 s)."""
    scenario = _app_call(w, "/api/scenarios")[0]["scenario_id"]
    session = _app_call(w, f"/api/scenarios/{scenario}/play", method="POST")["session_id"]
    deadline = time.monotonic() + FRESHNESS_WAIT_S
    while True:
        result = _app_call(w, f"/api/sessions/{session}/freshness")
        if result["status"] == "ready" or time.monotonic() > deadline:
            return result["status"] == "ready", {k: result.get(k) for k in ("status", "landed")} | {"session": session}


def main(config_path):
    cfg = load_config(config_path)
    w = WorkspaceClient(profile=cfg["workspace"]["profile"])
    checks = [("serving endpoints READY", lambda: endpoints_ready(w, cfg)),
              ("vector search endpoint + index", lambda: vector_search_online(w, cfg)),
              ("query embedding endpoint", lambda: query_embedding(w, cfg, config_path)),
              ("batch features current", lambda: batch_features_fresh(w, cfg)),
              ("personas lift (app)", lambda: personas_lift(w, cfg)),
              ("session freshness (app Play)", lambda: session_freshness(w, cfg))]
    failed = []
    for name, check in checks:
        try:
            ok, detail = check()
        except Exception as error:  # report every check, not just the first failure
            ok, detail = False, f"{type(error).__name__}: {str(error)[:300]}"
        print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}", flush=True)
        if not ok:
            failed.append(name)
    print(f"\n{'ALL PASS' if not failed else 'FAILED: ' + ', '.join(failed)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1]))
