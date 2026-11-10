# Databricks notebook source
# DBTITLE 1,QAC L1: prefix table in Lakebase
# MAGIC %md
# MAGIC # QAC L1: prefix table in Lakebase
# MAGIC
# MAGIC Builds the prefix -> top-k completions table the app reads on every keystroke:
# MAGIC
# MAGIC 1. **Completion stats** per normalized query from the train split of `qac_impressions`: popularity counts every
# MAGIC    submitted search (`pop_30d`, `pop_7d`), `ctr` is the engaged share, and `category`, `category_share`, `brand`,
# MAGIC    `brand_share` and `median_clicked_price` come from the products clicked or carted for it (`serp_impressions` +
# MAGIC    `products`).
# MAGIC 2. **Dead ends dropped:** completions with `ctr` below `lakebase.qac_min_ctr` are not served.
# MAGIC 3. **Prefix expansion** (`rts_ml.qac.expand_prefixes`) to the Delta table `qac_prefix_completions`, one row per
# MAGIC    (prefix, rank), with the columns in `rts_shared.retrieval`.
# MAGIC 4. **Lakebase synced table** (snapshot) on the shared instance, primary key `(prefix, rank)`.
# MAGIC
# MAGIC Checks: every one of the k most popular completions is served for each of its prefixes; Lakebase holds the
# MAGIC same rows as Delta and returns the same ranked completions for sampled prefixes. The lookup latency measured here
# MAGIC is indicative only (notebook network path, pure-Python driver); the p95 against the budget comes from the app
# MAGIC benchmark. `pg8000` because native libpq drivers (`psycopg`, even `[binary]`) can abort Python on serverless.
# MAGIC
# MAGIC Reruns are safe: the Delta table is overwritten and the synced table re-snapshotted.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-sdk>=0.81" "pg8000>=1.31" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import random
import sys
import time

from pyspark.sql import functions as F
from pyspark.sql import types as T
from pyspark.sql.window import Window

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.qac import drop_dead_ends, expand_prefixes
from rts_shared.config import load_config, table
from rts_shared.lakebase import connect, sync_snapshot
from rts_shared.retrieval import QAC_PREFIX_COLUMNS, QAC_PREFIX_PK, normalize

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
UC, LB = CFG["unity_catalog"], CFG["lakebase"]
MAX_LEN, K, MIN_CTR = LB["qac_max_prefix_len"], LB["qac_l1_k"], LB["qac_min_ctr"]
PREFIX_TABLE, ONLINE_TABLE = table(CFG, "qac_prefix_completions"), table(CFG, "qac_prefix_online")
w = WorkspaceClient()

# COMMAND ----------

# DBTITLE 1,Completion stats (train split)
train = spark.table(table(CFG, "qac_impressions")).where("split = 'train'")
ref = train.agg(F.max("event_time")).first()[0]  # windows end at the last train search, not today

# Normalize on the driver with the same function the app uses, so table keys and lookups agree.
queries = [r["query"] for r in train.select("query").distinct().collect()]
completion_of = spark.createDataFrame([(q, normalize(q)) for q in queries], "query string, completion string")
searches = train.join(completion_of, "query").where("completion != ''")

base = searches.groupBy("completion").agg(
    F.count_if(F.col("event_time") > F.lit(ref) - F.expr("INTERVAL 30 DAYS")).alias("pop_30d"),
    F.count_if(F.col("event_time") > F.lit(ref) - F.expr("INTERVAL 7 DAYS")).alias("pop_7d"),
    F.avg((F.col("outcome") == 2).cast("double")).alias("ctr"),
    F.mode("category").alias("query_category"))

clicks = (spark.table(table(CFG, "serp_impressions")).where("split = 'train' AND (clicked OR carted)")
          .select("search_id", "product_id")
          .join(searches.select("search_id", "completion"), "search_id")
          .join(spark.table(table(CFG, "products")).select("product_id", "category", "brand", "price"),
                "product_id"))
per_completion = Window.partitionBy("completion")
top_category = (clicks.groupBy("completion", "category").count()
                .withColumn("share", F.col("count") / F.sum("count").over(per_completion))
                .withColumn("r", F.row_number().over(per_completion.orderBy(F.desc("count"), "category")))
                .where("r = 1").select("completion", F.col("category").alias("clicked_category"),
                                       F.col("share").alias("category_share")))
top_brand = (clicks.where("brand IS NOT NULL").groupBy("completion", "brand").count()
             .withColumn("share", F.col("count") / F.sum("count").over(per_completion))
             .withColumn("r", F.row_number().over(per_completion.orderBy(F.desc("count"), "brand")))
             .where("r = 1").select("completion", "brand", F.col("share").alias("brand_share")))
clicked_price = clicks.groupBy("completion").agg(F.median("price").alias("median_clicked_price"))

stats_df = (base.join(top_category, "completion", "left").join(top_brand, "completion", "left")
            .join(clicked_price, "completion", "left")
            # No clicks: fall back to the query's own category; brand, shares and price stay null.
            .select("completion", "pop_30d", "pop_7d", "ctr",
                    F.coalesce("clicked_category", "query_category").alias("category"),
                    "category_share", "brand", "brand_share", "median_clicked_price"))
stats = [r.asDict() for r in stats_df.collect()]

ctr_quantiles = dict(zip(("p10", "p25", "p50", "p75", "p90"),
                         stats_df.approxQuantile("ctr", [0.1, 0.25, 0.5, 0.75, 0.9], 0.001)))
served = drop_dead_ends(stats, MIN_CTR)
print(f"{len(stats)} completions; ctr quantiles {ctr_quantiles}; {len(stats) - len(served)} below "
      f"min_ctr={MIN_CTR} dropped")

# COMMAND ----------

# DBTITLE 1,Prefix table (Delta)
rows = expand_prefixes(served, max_len=MAX_LEN, k=K)
schema = T.StructType([
    T.StructField("prefix", T.StringType(), False), T.StructField("rank", T.IntegerType(), False),
    T.StructField("completion", T.StringType(), False), T.StructField("prefix_len", T.IntegerType()),
    T.StructField("n_tokens", T.IntegerType()), T.StructField("is_word_boundary", T.BooleanType()),
    T.StructField("pop_30d", T.LongType()), T.StructField("pop_7d", T.LongType()),
    T.StructField("ctr", T.DoubleType()), T.StructField("category", T.StringType()),
    T.StructField("category_share", T.DoubleType()), T.StructField("brand", T.StringType()),
    T.StructField("brand_share", T.DoubleType()), T.StructField("median_clicked_price", T.DoubleType())])
assert tuple(schema.names) == QAC_PREFIX_COLUMNS
(spark.createDataFrame([tuple(r[c] for c in QAC_PREFIX_COLUMNS) for r in rows], schema)
 .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(PREFIX_TABLE))
delta_rows = spark.table(PREFIX_TABLE).count()
print(f"{PREFIX_TABLE}: {delta_rows} rows, {len({r['prefix'] for r in rows})} prefixes")

# COMMAND ----------

# DBTITLE 1,Check: the k most popular completions are served for every one of their prefixes
by_prefix = {}
for r in rows:
    by_prefix.setdefault(r["prefix"], set()).add(r["completion"])
head = sorted(served, key=lambda s: (-s["pop_30d"], s["completion"]))[:K]
missing = [(s["completion"], n) for s in head for n in range(1, min(MAX_LEN, len(s["completion"])) + 1)
           if s["completion"] not in by_prefix.get(s["completion"][:n], ())]
assert not missing, f"top completions not served for their own prefixes: {missing[:10]}"

# COMMAND ----------

# DBTITLE 1,Lakebase synced table (snapshot)
status = sync_snapshot(w, CFG, PREFIX_TABLE, ONLINE_TABLE, list(QAC_PREFIX_PK))
print(ONLINE_TABLE, status.detailed_state, "pipeline", status.pipeline_id)

online_rows = spark.table(ONLINE_TABLE).count()
assert online_rows == delta_rows, f"Lakebase has {online_rows} rows, Delta {delta_rows}"

# COMMAND ----------

# DBTITLE 1,Check: Lakebase lookups match Delta; indicative latency
pg_table = f'"{UC["schema"]}"."{CFG["tables"]["qac_prefix_online"]}"'
LOOKUP = f"SELECT completion FROM {pg_table} WHERE prefix = :prefix ORDER BY rank"

expected = {}
for r in sorted(rows, key=lambda r: r["rank"]):
    expected.setdefault(r["prefix"], []).append(r["completion"])
rng = random.Random(0)
sample = rng.sample(sorted(expected), min(200, len(expected)))

conn = connect(w, CFG)
try:
    mismatched = [p for p in sample if [c for (c,) in conn.run(LOOKUP, prefix=p)] != expected[p]]
    timings = []
    for p in rng.choices(sample, k=500):
        t = time.perf_counter()
        conn.run(LOOKUP, prefix=p)
        timings.append((time.perf_counter() - t) * 1000)
finally:
    conn.close()
assert not mismatched, f"{len(mismatched)}/{len(sample)} prefixes differ from Delta, e.g. {mismatched[:5]}"
timings.sort()
latency = {"n": len(timings), "p50_ms": round(timings[len(timings) // 2], 2),
           "p95_ms": round(timings[int(len(timings) * 0.95)], 2), "max_ms": round(timings[-1], 2)}
print("lookup latency from this notebook (indicative):", latency)

# COMMAND ----------

# DBTITLE 1,Summary
summary = {
    "completions": len(stats), "served": len(served), "min_ctr": MIN_CTR, "ctr_quantiles": ctr_quantiles,
    "served_with_brand": sum(s["brand"] is not None for s in served),
    "prefix_rows": delta_rows, "prefixes": len(expected), "online_table": ONLINE_TABLE,
    "lookups_checked": len(sample), "lookup_latency_indicative": latency,
}
print(json.dumps(summary, indent=2, default=str))
dbutils.notebook.exit(json.dumps(summary, default=str))
