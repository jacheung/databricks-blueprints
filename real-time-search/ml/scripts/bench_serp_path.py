"""End-to-end latency of the whole SERP path, outside the app: embed -> hybrid L1 top 200 -> SERP L2 (DCNv2, T4).

A baseline to compare the in-app numbers against. Makes the same calls as the app's `/api/search`: the query on
`rts-query-embedding`, `query_index` (HYBRID, `query_text` + `query_vector`, keyword half on `search_text`,
`SERP_INDEX_COLUMNS`), then the hits through `serp_l2_records` to `models.serp_l2.endpoint`. Route-optimized
endpoints accept only OAuth tokens, so this runs locally with the SDK and a CLI OAuth login. From this folder's parent:

    uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I ml/scripts/bench_serp_path.py \\
        shared/configs/dev.yml

Requests: distinct logged-in test-split searches (`serp_l2_searches`), one call each, in sequence. Each stage includes
this machine's round trip to the workspace, so absolute numbers are indicative; the budget check is the app's.
"""

from __future__ import annotations

import json
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared", "src"))
from rts_shared.config import full_name, load_config  # noqa: E402
from rts_shared.retrieval import SERP_INDEX_COLUMNS, SERP_INDEX_TEXT, serp_l2_records  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

WARMUP, N_SEARCHES = 5, 100


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def sample_searches(w, cfg, n):
    warehouse = next(iter(w.warehouses.list())).id  # any warehouse; serverless ones start on demand
    sql = (f"SELECT session_id, user_id, query FROM {full_name(cfg, 'serp_l2_searches')} "
           f"WHERE split = 'test' AND user_id IS NOT NULL ORDER BY sha2(search_id, 256) LIMIT {n}")
    return w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse,
                                                   wait_timeout="50s").result.data_array


def main(config_path):
    cfg = load_config(config_path)
    ai, l2 = cfg["ai_search"], cfg["models"]["serp_l2"]
    embed_ep, index = cfg["models"]["query_embedding"]["endpoint"], full_name(cfg, ai["index"])
    w = WorkspaceClient(profile=cfg["workspace"]["profile"])
    searches = sample_searches(w, cfg, WARMUP + N_SEARCHES)

    def one(session_id, user_id, query):
        t0 = time.perf_counter()
        vector = w.serving_endpoints_data_plane.query(name=embed_ep, dataframe_records=[{"text": query}]).predictions[0]
        t1 = time.perf_counter()
        r = w.vector_search_indexes.query_index(index_name=index, columns=list(SERP_INDEX_COLUMNS), query_text=query,
                                                query_vector=vector, query_type="HYBRID", query_columns=[SERP_INDEX_TEXT],
                                                num_results=ai["serp_l1_k"])
        hits = [dict(zip([c.name for c in r.manifest.columns], row)) for row in r.result.data_array or []]
        t2 = time.perf_counter()
        scores = w.serving_endpoints_data_plane.query(
            name=l2["endpoint"], dataframe_records=serp_l2_records(hits, session_id, user_id)).predictions
        t3 = time.perf_counter()
        assert len(scores) == len(hits), f"{len(scores)} scores for {len(hits)} hits ({query!r})"
        return len(hits), {"embed": t1 - t0, "l1_search": t2 - t1, "l2": t3 - t2, "total": t3 - t0}

    for s in searches[:WARMUP]:
        one(*s)
    times, n_hits = {k: [] for k in ("embed", "l1_search", "l2", "total")}, []
    for s in searches[WARMUP:]:
        n, t = one(*s)
        n_hits.append(n)
        for k, v in t.items():
            times[k].append(v * 1000)

    report = {"searches": len(n_hits), "hits_min": min(n_hits), "hits_mean": round(sum(n_hits) / len(n_hits), 1),
              "l2_endpoint": l2["endpoint"],
              **{k: {"p50": round(percentile(v, 0.5), 1), "p95": round(percentile(v, 0.95), 1),
                     "max": round(max(v), 1)} for k, v in times.items()}}
    print(json.dumps(report, indent=2))
    print("OK")


if __name__ == "__main__":
    main(sys.argv[1])
