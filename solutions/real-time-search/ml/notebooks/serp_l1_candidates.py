# Databricks notebook source
# DBTITLE 1,SERP L1 candidates for L2 training and evaluation
# MAGIC %md
# MAGIC # SERP L1 candidates for L2 training and evaluation
# MAGIC
# MAGIC SERP L2 reranks what L1 returns, so it trains on that: each sampled search's hybrid top `ai_search.serp_l1_k`.
# MAGIC Nothing stores those lists (`serp_impressions` holds only the ESCI-judged products the simulator showed), so
# MAGIC this notebook asks the index once per distinct query and keeps the answer:
# MAGIC
# MAGIC | Table | Grain | Used by |
# MAGIC |---|---|---|
# MAGIC | `serp_l2_searches` | one row per sampled search: `models.serp_l2.train_searches` train + `eval_searches` test, engaged only | `serp_l2_training_set` |
# MAGIC | `serp_l1_candidates` | one row per (query, rank): `product_id`, hybrid `score` | `serp_l2_training_set` (labels, `l1_rank`, `l1_score`), step 7 |
# MAGIC
# MAGIC **Engaged searches only** (a click or cart): a search with no engaged product has no NDCG and teaches the listwise
# MAGIC loss nothing. The index is queried like `/api/search`: HYBRID, keyword half on `search_text`. The app passes its own
# MAGIC T4 query vector; the index's managed embedding gives the same results (step 3 parity check), so text is enough here.
# MAGIC Frozen lists also make L1, LightGBM and DCNv2 compare on identical candidates (hybrid's tail varies slightly).
# MAGIC
# MAGIC Reruns resume: queries already in `serp_l1_candidates` are skipped. Set `rebuild=true` to resample and requery.
# MAGIC
# MAGIC Checks: the index returns a `score`; ≥ 95% of queries get the full k; recall@k of ESCI Exact products on the
# MAGIC sampled test queries is ≥ 0.80 (step 3 measured 0.876 on uniformly sampled test queries; this sample is
# MAGIC popularity-weighted, so it's reported, not matched).
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+. Queries `ai_search.index` (~10K calls, 8 threads).

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-sdk>=0.81" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor

from pyspark.sql import functions as F

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.serp import recall_at_k
from rts_shared.config import full_name, load_config, table
from rts_shared.retrieval import SERP_INDEX_PK, SERP_INDEX_TEXT

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
dbutils.widgets.dropdown("rebuild", "false", ["false", "true"])  # resample searches and requery every query
CFG = load_config(dbutils.widgets.get("config_path"))
AS, L2 = CFG["ai_search"], CFG["models"]["serp_l2"]
K, INDEX = AS["serp_l1_k"], full_name(CFG, AS["index"])
SEARCHES, CANDIDATES = table(CFG, "serp_l2_searches"), table(CFG, "serp_l1_candidates")
w = WorkspaceClient()

# COMMAND ----------

# DBTITLE 1,Sample engaged searches (train + test)
rebuild = dbutils.widgets.get("rebuild") == "true"
if rebuild or not spark.catalog.tableExists(SEARCHES):
    engaged = spark.table(table(CFG, "qac_impressions")).where("outcome = 2")  # one row per search; 2 = click/cart


    def sample(split, n):  # deterministic: the same searches every rebuild
        return engaged.where(F.col("split") == split).orderBy(F.sha2(F.col("search_id"), 256)).limit(n)


    (sample("train", L2["train_searches"]).unionByName(sample("test", L2["eval_searches"]))
     .select("search_id", "session_id", "user_id", "event_time", "query_id", "query", "split")
     .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(SEARCHES))
searches = spark.table(SEARCHES)
print({r["split"]: r["count"] for r in searches.groupBy("split").count().collect()})

# COMMAND ----------

# DBTITLE 1,Query the index once per distinct query (resumable)
if rebuild:
    spark.sql(f"DROP TABLE IF EXISTS {CANDIDATES}")
done = (set(r["query_id"] for r in spark.table(CANDIDATES).select("query_id").distinct().collect())
        if spark.catalog.tableExists(CANDIDATES) else set())
todo = sorted((r["query_id"], r["query"]) for r in searches.select("query_id", "query").distinct().collect()
              if r["query_id"] not in done)
print(f"{len(done)} queries already fetched, {len(todo)} to go")


def hybrid(query):
    """Top K like /api/search: (product_id, score) in rank order. Retries rate limits with backoff."""
    for attempt in range(6):
        try:
            res = w.vector_search_indexes.query_index(index_name=INDEX, columns=[SERP_INDEX_PK], query_text=query,
                                                      query_type="HYBRID", query_columns=[SERP_INDEX_TEXT],
                                                      num_results=K)
            names = [c.name for c in res.manifest.columns]
            assert "score" in names, f"index returned no score column: {names}"
            pk, sc = names.index(SERP_INDEX_PK), names.index("score")
            return [(row[pk], float(row[sc])) for row in (res.result.data_array or [])]
        except Exception as e:
            if "429" not in str(e) and "RESOURCE_EXHAUSTED" not in str(e) and "rate" not in str(e).lower():
                raise
            time.sleep(2 ** attempt)
    raise RuntimeError(f"rate-limited after retries: {query!r}")


SCHEMA = "query_id bigint, rank int, product_id string, score double"
CHUNK = 500  # append every CHUNK queries, so an interrupted run keeps what it fetched
t0 = time.time()
with ThreadPoolExecutor(8) as pool:
    for start in range(0, len(todo), CHUNK):
        chunk = todo[start:start + CHUNK]
        hits = list(pool.map(lambda q: hybrid(q[1]), chunk))
        rows = [(qid, rank, pid, score) for (qid, _), h in zip(chunk, hits)
                for rank, (pid, score) in enumerate(h, start=1)]
        if rows:
            spark.createDataFrame(rows, SCHEMA).write.mode("append").saveAsTable(CANDIDATES)
        print(f"{start + len(chunk)}/{len(todo)} queries, {time.time() - t0:.0f} s")

# COMMAND ----------

# DBTITLE 1,Checks: full lists, recall of Exact products on the test queries
candidates = spark.table(CANDIDATES)
per_query = candidates.groupBy("query_id").agg(F.count("*").alias("n"), F.countDistinct("product_id").alias("distinct"),
                                               F.max("rank").alias("max_rank"))
stats = per_query.agg(F.count("*").alias("queries"), F.avg((F.col("n") == K).cast("double")).alias("full_share"),
                      F.min("n").alias("min_n"), F.max(F.col("n") - F.col("distinct")).alias("max_dupes"),
                      F.max(F.col("max_rank") - F.col("n")).alias("max_rank_gap")).first().asDict()
needed = searches.select("query_id").distinct().count()

test_queries = searches.where("split = 'test'").select("query_id").distinct()
exact = (spark.table(table(CFG, "labels")).where("esci_label = 'E'").join(test_queries, "query_id")
         .groupBy("query_id").agg(F.collect_set("product_id").alias("exact")))
retrieved = candidates.join(test_queries, "query_id").groupBy("query_id").agg(
    F.sort_array(F.collect_list(F.struct("rank", "product_id"))).alias("hits"))
recalls = [recall_at_k([h["product_id"] for h in r["hits"]], set(r["exact"]), (50, 100, K))
           for r in exact.join(retrieved, "query_id").collect()]
recall = {f"recall@{k}": round(sum(x[k] for x in recalls) / len(recalls), 4) for k in (50, 100, K)}

summary = {"searches": {r["split"]: r["count"] for r in searches.groupBy("split").count().collect()},
           "queries_needed": needed, **{k: (round(v, 4) if isinstance(v, float) else v) for k, v in stats.items()},
           "candidate_rows": candidates.count(), "test_queries_with_exact": len(recalls), **recall}
print(json.dumps(summary, indent=2))
assert stats["queries"] == needed, f"{needed - stats['queries']} sampled queries have no candidates"
assert stats["max_dupes"] == 0 and stats["max_rank_gap"] == 0, "duplicate products or rank gaps in a query's list"
assert stats["full_share"] >= 0.95, f"only {stats['full_share']:.1%} of queries returned {K} candidates"
assert recall[f"recall@{K}"] >= 0.80, f"recall@{K} {recall[f'recall@{K}']} is far below step 3's 0.876"
dbutils.notebook.exit(json.dumps(summary))
