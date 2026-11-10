"""End-to-end latency of the SERP L2 endpoints: DCNv2 on GPU (`models.serp_l2`) vs LightGBM on CPU (`.lgbm`).

Route-optimized endpoints accept only OAuth tokens, which notebooks can't provide, so this runs locally with the
SDK and a CLI OAuth login (`workspace.profile` in the config). From this folder's parent:

    uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I ml/scripts/bench_serp_l2.py \\
        shared/configs/dev.yml

Requests are one real test-split search's L1 candidates (`serp_l2_spine`), built with the app's
`rts_shared.retrieval.serp_l2_records`: 200 (L1's k) and 500 (the 200 repeated, ranks continued). Each endpoint looks
up the session, user and item features itself. Checks first: every candidate gets a score, and the same request as a
guest scores differently (user lookups work). Latency includes this machine's round trip to the workspace, the same
for both endpoints, so the comparison holds but absolute numbers are indicative; the budget check is the app's.
"""

from __future__ import annotations

import json
import os
import statistics
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared", "src"))
from rts_shared.config import full_name, load_config  # noqa: E402
from rts_shared.retrieval import serp_l2_records  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

WARMUP, N_CALLS, SIZES = 5, 100, (200, 500)


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def one_search(w, cfg):
    """A logged-in test search with an earlier search in its session, and its L1 candidates in rank order."""
    warehouse = next(iter(w.warehouses.list())).id  # any warehouse; serverless ones start on demand
    spine = full_name(cfg, "serp_l2_spine")
    sql = (f"WITH s AS (SELECT search_id FROM {spine} WHERE split = 'test' AND user_id IS NOT NULL "
           f"GROUP BY search_id ORDER BY sha2(search_id, 256) LIMIT 1) "
           f"SELECT session_id, user_id, product_id, l1_rank, l1_score, price, brand, category "
           f"FROM {spine} JOIN s USING (search_id) ORDER BY l1_rank")
    rows = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse,
                                                   wait_timeout="50s").result.data_array
    hits = [{"product_id": r[2], "score": float(r[4]), "price": None if r[5] is None else float(r[5]),
             "brand": r[6], "category": r[7]} for r in rows]
    return rows[0][0], rows[0][1], hits


def main(config_path):
    cfg = load_config(config_path)
    l2 = cfg["models"]["serp_l2"]
    endpoints = {"dcnv2_gpu": l2["endpoint"], "lgbm_cpu": l2["lgbm"]["endpoint"]}
    w = WorkspaceClient(profile=cfg["workspace"]["profile"])
    session_id, user_id, hits = one_search(w, cfg)
    requests = {n: serp_l2_records((hits * (n // len(hits) + 1))[:n], session_id, user_id) for n in SIZES}

    def query(name, records):
        return w.serving_endpoints_data_plane.query(name=name, dataframe_records=records).predictions

    results = {"session_id": session_id, "user_id": user_id, "candidates": len(hits)}
    for label, name in endpoints.items():
        scores = query(name, requests[200])
        assert len(scores) == 200, f"{name} returned {len(scores)} scores for 200 candidates"
        guest = query(name, serp_l2_records(hits, session_id, None))
        assert scores != guest, f"{name}: logged-in and guest requests score the same; user lookups not applied?"
        for n, records in requests.items():
            for _ in range(WARMUP):
                query(name, records)
            times = []
            for _ in range(N_CALLS):
                t = time.perf_counter()
                query(name, records)
                times.append((time.perf_counter() - t) * 1000)
            results[f"{label}_{n}"] = {"p50": round(percentile(times, 0.5), 1), "p95": round(percentile(times, 0.95), 1),
                                       "mean": round(statistics.mean(times), 1)}
            print(label, n, results[f"{label}_{n}"], flush=True)
    print(json.dumps(results, indent=2))
    print("OK")


if __name__ == "__main__":
    main(sys.argv[1])
