# Databricks notebook source
# DBTITLE 1,SERP L1: hybrid index over products
# MAGIC %md
# MAGIC # SERP L1: hybrid index over products
# MAGIC
# MAGIC Builds the AI Search (Vector Search) index the app retrieves SERP candidates from:
# MAGIC
# MAGIC 1. **Source table** `products_search`: the columns in `rts_shared.retrieval` plus `search_text`, the cleaned title
# MAGIC    with the brand appended when the title doesn't name it (`rts_ml.serp.search_text`). Category stays out of the
# MAGIC    text: its labels are LLM-assigned and its words ("home", "kitchen") match too broadly.
# MAGIC 2. **Standard endpoint** (`ai_search.endpoint`): 20-50 ms queries; storage-optimized would take most of the SERP
# MAGIC    budget on its own.
# MAGIC 3. **Delta Sync index** with managed embeddings (`ai_search.embedding_endpoint`, pay-per-token) on `search_text`.
# MAGIC    Hybrid queries run vector and keyword (BM25) search and merge them with reciprocal rank fusion; keyword search
# MAGIC    is limited to `search_text` (`query_columns`) so a brand match counts once.
# MAGIC
# MAGIC Evaluation: recall@50/100/200 against ESCI Exact labels for `ai_search.eval_queries` sampled test-split queries,
# MAGIC hybrid vs vector-only. No bar is asserted yet; it is picked after the first measurement.
# MAGIC Query latency measured here is indicative only (notebook network path).
# MAGIC
# MAGIC Reruns are safe: the source table is overwritten and the index re-synced.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+. The endpoint bills while it exists.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-sdk>=0.81" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import random
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from pyspark.sql import functions as F
from pyspark.sql import types as T

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.serp import recall_at_k, search_text
from rts_shared.config import full_name, load_config, table
from rts_shared.retrieval import SERP_INDEX_COLUMNS, SERP_INDEX_PK, SERP_INDEX_TEXT

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound
from databricks.sdk.service.vectorsearch import (DeltaSyncVectorIndexSpecRequest, EmbeddingSourceColumn, EndpointType,
                                                 PipelineType, VectorIndexType)

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
AS = CFG["ai_search"]
K, KS = AS["serp_l1_k"], (50, 100, 200)
SOURCE, INDEX = table(CFG, "products_search"), full_name(CFG, AS["index"])
w = WorkspaceClient()

# COMMAND ----------

# DBTITLE 1,Endpoint (Standard)
# First: the workspace endpoint quota is shared, so claim a slot before the long source-table build.
try:
    w.vector_search_endpoints.get_endpoint(endpoint_name=AS["endpoint"])
except NotFound:
    w.vector_search_endpoints.create_endpoint(name=AS["endpoint"], endpoint_type=EndpointType.STANDARD)
for _ in range(60):  # bounded: long blocking polls can outlive a serverless session
    ep = w.vector_search_endpoints.get_endpoint(endpoint_name=AS["endpoint"])
    state = str(ep.endpoint_status.state)
    if state.endswith("ONLINE"):
        break
    assert "OFFLINE" not in state, f"endpoint {state}: {ep.endpoint_status.message}"
    time.sleep(30)
print(AS["endpoint"], ep.endpoint_type, state)

# COMMAND ----------

# DBTITLE 1,Source table
# Built on the driver with the same function the tests cover (executors can't import rts_ml on serverless).
products = spark.table(table(CFG, "products")).select(*SERP_INDEX_COLUMNS)
pdf = products.toPandas()  # via Arrow; row-by-row Python tuples took 10+ minutes for 170K products
n_products = len(pdf)
pdf[SERP_INDEX_TEXT] = [search_text(t, b) for t, b in zip(pdf["title"], pdf["brand"])]
schema = T.StructType([*products.schema.fields, T.StructField(SERP_INDEX_TEXT, T.StringType())])
# Same schema on every run: a schema change would break the index's sync.
spark.createDataFrame(pdf, schema).write.mode("overwrite").saveAsTable(SOURCE)
spark.sql(f"ALTER TABLE {SOURCE} SET TBLPROPERTIES (delta.enableChangeDataFeed = true)")  # Delta Sync needs CDF
print(SOURCE, spark.table(SOURCE).count(), "rows; e.g.", spark.table(SOURCE).select(SERP_INDEX_TEXT).first()[0])

empty = spark.table(SOURCE).where(F.length(SERP_INDEX_TEXT) == 0).count()
assert empty == 0, f"{empty} products have no search text"

# COMMAND ----------

# DBTITLE 1,Index (Delta Sync, managed embeddings)
try:
    w.vector_search_indexes.get_index(index_name=INDEX)
    w.vector_search_indexes.sync_index(index_name=INDEX)
except NotFound:
    w.vector_search_indexes.create_index(
        name=INDEX, endpoint_name=AS["endpoint"], primary_key=SERP_INDEX_PK, index_type=VectorIndexType.DELTA_SYNC,
        delta_sync_index_spec=DeltaSyncVectorIndexSpecRequest(
            source_table=SOURCE, pipeline_type=PipelineType.TRIGGERED,
            columns_to_sync=[*SERP_INDEX_COLUMNS, SERP_INDEX_TEXT],
            embedding_source_columns=[EmbeddingSourceColumn(
                name=SERP_INDEX_TEXT, embedding_model_endpoint_name=AS["embedding_endpoint"])]))

deadline = time.time() + 3 * 3600
while not ((st := w.vector_search_indexes.get_index(index_name=INDEX).status).ready
           and st.indexed_row_count == n_products):
    assert time.time() < deadline, f"index not ready: {st}"
    time.sleep(60)
print(INDEX, "ready,", st.indexed_row_count, "rows")

# COMMAND ----------

# DBTITLE 1,Evaluation set: sampled test-split queries with Exact labels
test_ids = spark.table(table(CFG, "qac_impressions")).where("split = 'test'").select("query_id").distinct()
exact = (spark.table(table(CFG, "labels")).where("esci_label = 'E'").join(test_ids, "query_id")
         .groupBy("query_id").agg(F.collect_set("product_id").alias("exact"))
         .join(spark.table(table(CFG, "queries")).select("query_id", "query"), "query_id"))
pool = sorted((r["query_id"], r["query"], set(r["exact"])) for r in exact.collect())
sample = random.Random(0).sample(pool, min(AS["eval_queries"], len(pool)))
print(f"{len(pool)} test queries with Exact labels; evaluating {len(sample)}")

# COMMAND ----------

# DBTITLE 1,Recall@k: hybrid vs vector-only
QUERY_COLUMNS = [SERP_INDEX_TEXT]


def search(query, mode):
    kw = {"query_columns": QUERY_COLUMNS} if mode == "HYBRID" and QUERY_COLUMNS else {}
    for attempt in range(6):
        try:
            t = time.perf_counter()
            res = w.vector_search_indexes.query_index(index_name=INDEX, columns=[SERP_INDEX_PK], query_text=query,
                                                      query_type=mode, num_results=K, **kw)
            ms = (time.perf_counter() - t) * 1000
            return [row[0] for row in (res.result.data_array or [])], ms
        except Exception as e:
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e) and "rate" not in str(e).lower():
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"rate-limited after retries: {query!r}")


# The app reads the contract columns from the index; check they come back.
probe = w.vector_search_indexes.query_index(index_name=INDEX, columns=list(SERP_INDEX_COLUMNS),
                                            query_text=sample[0][1], query_type="HYBRID", num_results=1)
returned = [c.name for c in probe.manifest.columns]
assert set(SERP_INDEX_COLUMNS) <= set(returned), f"index returns {returned}"

# Keyword search limited to search_text needs the Full-Text Search preview; without it, every text column is matched.
try:
    search(sample[0][1], "HYBRID")
    keyword_columns = QUERY_COLUMNS[:]
except Exception as e:
    print("query_columns not accepted, keyword search covers all synced text columns:", str(e)[:300])
    QUERY_COLUMNS, keyword_columns = [], "all synced text columns"


def pct(xs, q):
    xs = sorted(xs)
    return round(xs[min(len(xs) - 1, int(len(xs) * q))], 1)


results = {}
for mode in ("HYBRID", "ANN"):
    with ThreadPoolExecutor(8) as pool_:
        out = list(pool_.map(lambda s: search(s[1], mode), sample))
    recalls = [recall_at_k(ids, s[2], KS) for (ids, _), s in zip(out, sample)]
    lat = [ms for _, ms in out]
    results[mode.lower()] = {
        **{f"recall@{k}": round(sum(r[k] for r in recalls) / len(recalls), 4) for k in KS},
        "queries_with_no_exact_in_top_200": sum(r[200] == 0 for r in recalls),
        "returned_p50": sorted(len(ids) for ids, _ in out)[len(out) // 2],
        "latency_ms_indicative": {"p50": pct(lat, 0.5), "p95": pct(lat, 0.95)}}
    print(mode, results[mode.lower()])

# COMMAND ----------

# DBTITLE 1,Summary
summary = {"products": n_products, "endpoint": AS["endpoint"], "index": INDEX,
           "embedding_endpoint": AS["embedding_endpoint"], "k": K, "keyword_columns": keyword_columns,
           "eval_queries": len(sample), "test_queries_with_exact": len(pool), "results": results}
print(json.dumps(summary, indent=2, default=str))
dbutils.notebook.exit(json.dumps(summary, default=str))
