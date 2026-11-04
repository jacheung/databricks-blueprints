"""Check the custom query-embedding endpoint can stand in for AI Search's managed (pay-per-token) embeddings.

Route-optimized endpoints accept only OAuth tokens, which notebooks can't provide, so this runs locally with the
SDK and a CLI OAuth login (`workspace.profile` in the config). From this folder's parent:

    uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I ml/scripts/check_query_embedding.py \\
        shared/configs/dev.yml

Checks, on sampled test-split queries: custom and pay-per-token vectors match in direction (cosine ~1) and length
(pay-per-token GTE is unnormalized, norm ~24, and the index ranks by L2), and hybrid search with
`query_text` + the custom vector returns the same products as `query_text` alone. Latency printed here includes this
machine's round trip to the workspace, so it's indicative only; the budget check is the app benchmark.
"""

from __future__ import annotations

import math
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "..", "shared", "src"))
from rts_shared.config import full_name, load_config, table  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402

MIN_COSINE, MAX_NORM_GAP, N_QUERIES, N_SEARCHES = 0.999, 0.01, 200, 20
# Hybrid top-200 isn't deterministic at the tail: the same text query run twice overlaps 0.990 min / 0.9992 mean
# (20 queries, 2026-11-02), so result overlap is a sanity check just outside that noise, not exact equality.
MIN_OVERLAP, MIN_OVERLAP_MEAN = 0.97, 0.99


def cosine(a, b):
    return sum(x * y for x, y in zip(a, b)) / (math.sqrt(sum(x * x for x in a)) * math.sqrt(sum(y * y for y in b)))


def norm(v):
    return math.sqrt(sum(x * x for x in v))


def percentile(values, q):
    values = sorted(values)
    return values[min(len(values) - 1, int(len(values) * q))]


def sample_queries(w, cfg, n):
    warehouse = next(iter(w.warehouses.list())).id  # any warehouse; serverless ones start on demand
    sql = (f"SELECT DISTINCT query FROM {table(cfg, 'qac_impressions')} WHERE split = 'test' "
           f"ORDER BY sha2(query, 256) LIMIT {n}")
    result = w.statement_execution.execute_statement(statement=sql, warehouse_id=warehouse, wait_timeout="50s")
    return [row[0] for row in result.result.data_array]


def main(config_path):
    cfg = load_config(config_path)
    qe, ai = cfg["models"]["query_embedding"], cfg["ai_search"]
    w = WorkspaceClient(profile=cfg["workspace"]["profile"])
    queries = sample_queries(w, cfg, N_QUERIES)

    def custom(texts):
        return w.serving_endpoints_data_plane.query(name=qe["endpoint"],
                                                    dataframe_records=[{"text": t} for t in texts]).predictions

    def ppt(texts):
        return [d.embedding for d in w.serving_endpoints.query(name=ai["embedding_endpoint"], input=texts).data]

    custom(["warm-up"])
    timings, cosines, norm_gaps, vectors = [], [], [], {}
    for q in queries:
        t = time.perf_counter()
        (vec,) = custom([q])
        timings.append((time.perf_counter() - t) * 1000)
        vectors[q] = vec
    for i in range(0, len(queries), 16):
        batch = queries[i:i + 16]
        for q, v in zip(batch, ppt(batch)):
            # Both direction and length: the index ranks by L2 distance over unnormalized pay-per-token vectors,
            # so a unit-normalized query vector points the same way and still reorders the results.
            cosines.append(cosine(vectors[q], v))
            norm_gaps.append(abs(norm(vectors[q]) / norm(v) - 1))

    index = full_name(cfg, ai["index"])
    overlaps = []
    for q in queries[:N_SEARCHES]:
        def ids(**kw):
            r = w.vector_search_indexes.query_index(index_name=index, columns=["product_id"], query_text=q,
                                                    query_type="HYBRID", num_results=ai["serp_l1_k"], **kw)
            return [row[0] for row in r.result.data_array]
        a, b = ids(), ids(query_vector=vectors[q])
        overlaps.append(len(set(a) & set(b)) / max(len(a), 1))

    report = {"queries": len(queries), "dim": len(vectors[queries[0]]), "cosine_min": round(min(cosines), 6),
              "cosine_mean": round(sum(cosines) / len(cosines), 6), "norm_gap_max": round(max(norm_gaps), 4),
              "hybrid_overlap_min": round(min(overlaps), 3), "hybrid_overlap_mean": round(sum(overlaps) / len(overlaps), 3),
              "custom_embed_ms_indicative": {"p50": round(percentile(timings, 0.5)), "p95": round(percentile(timings, 0.95)),
                                             "max": round(max(timings))}}
    print(report)
    assert report["cosine_min"] >= MIN_COSINE, f"custom vectors drift from pay-per-token: {report}"
    assert report["norm_gap_max"] <= MAX_NORM_GAP, f"custom vector lengths differ (normalized?): {report}"
    assert report["hybrid_overlap_min"] >= MIN_OVERLAP and report["hybrid_overlap_mean"] >= MIN_OVERLAP_MEAN, \
        f"hybrid results change with the custom vector: {report}"
    print("OK: the custom endpoint can replace managed query embeddings")


if __name__ == "__main__":
    main(sys.argv[1])
