# Databricks notebook source
# DBTITLE 1,Build the product catalog from Amazon ESCI
# MAGIC %md
# MAGIC # Build the product catalog from Amazon ESCI
# MAGIC
# MAGIC Downloads the [Shopping Queries Dataset (ESCI)](https://github.com/amazon-science/esci-data) (Apache-2.0)
# MAGIC into the config's volume, pinned to one commit and checked against its sha256 (`rts_data.sources`), and writes
# MAGIC three tables for the US locale:
# MAGIC
# MAGIC | Table | Grain | Notes |
# MAGIC |---|---|---|
# MAGIC | `queries` | one row per query | `category` from `ai_classify` over the config's categories |
# MAGIC | `esci_labels` | one row per (query, product) judgement | E/S/C/I label and NDCG gain |
# MAGIC | `products` | one row per judged product | `category` and synthetic `price` (ESCI has neither) |
# MAGIC
# MAGIC **Category:** classifying ~97K queries is ~10x fewer LLM calls than classifying ~1M product titles. A product
# MAGIC takes the majority category of the queries it is an Exact (weight 1) or Substitute (0.4) match for; products
# MAGIC with only Complement/Irrelevant judgements are classified from their title.
# MAGIC **Price:** lognormal around the category's median price, fixed per product so reruns match.
# MAGIC
# MAGIC `data.max_queries` caps the run to a deterministic subset of queries (null = all ~97K) for fast iteration.
# MAGIC
# MAGIC **Category snapshot:** `ai_classify` is an LLM, so labels can differ between runs and as its model changes.
# MAGIC Its answers are committed in `data/snapshots/` (id, category), and rows found there take that label, so a rebuild
# MAGIC anywhere gets the same categories. Only rows missing from it (e.g. the full ~97K-query run) go to `ai_classify`;
# MAGIC export them with `data/scripts/export_category_snapshot.py` and commit. After changing `categories` or
# MAGIC `classify_instructions`, run with `use_category_snapshot=false` (it fails on a label the config no longer has).
# MAGIC
# MAGIC **Resumable classification:** `ai_classify` is the slow, costly step, so it runs as a Structured Streaming
# MAGIC `availableNow` job over a staging table split into files of `data.classify_batch_rows` rows, one file per
# MAGIC micro-batch, into `<table>_llm`. A failed or cancelled run resumes from the last committed batch on rerun; a
# MAGIC finished one is a no-op. Set `reset_classification=true` after changing the rows to classify (`max_queries`,
# MAGIC `categories`, `classify_instructions`, or the snapshot).
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import functools
import json
import math
import os
import shutil
import sys
import urllib.request

from pyspark.sql import DataFrame
from pyspark.sql import functions as F
from pyspark.sql import types as T

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_data import datagen
from rts_data.datagen import ESCI_GAIN, category_vote, synthetic_price
from rts_data.sources import ESCI_FILES, esci_url, read_category_snapshot, sha256_file
from rts_shared.config import (category_medians, classify_labels, full_name, load_config, sql_string, table,
                               volume_path)
from pyspark import cloudpickle

cloudpickle.register_pickle_by_value(datagen)  # workers can't import from the notebook's folder

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
dbutils.widgets.dropdown("reset_classification", "false", ["false", "true"])  # redo ai_classify from scratch
# true: categories come from data/snapshots/ where it has the row, ai_classify only for the rest; false: all ai_classify
dbutils.widgets.dropdown("use_category_snapshot", "true", ["true", "false"])
CFG = load_config(dbutils.widgets.get("config_path"))
DATA = CFG["data"]
MEDIANS = category_medians(CFG)
# Labels carry a description each, plus instructions, so overlapping departments split consistently.
CLASSIFY_ARGS = (f"{sql_string(classify_labels(CFG))}, "
                 f"map('version', '2.0', 'instructions', {sql_string(DATA['classify_instructions'])})")
RESET = dbutils.widgets.get("reset_classification") == "true"
USE_SNAPSHOT = dbutils.widgets.get("use_category_snapshot") == "true"
SNAPSHOTS = f"{ROOT}/data/snapshots"
CHECKPOINTS = f"{volume_path(CFG)}/_checkpoints"

# COMMAND ----------

# DBTITLE 1,Download ESCI into the volume
# Pinned to a commit and checked against its Git LFS sha256 (rts_data.sources), so every rebuild reads the same bytes.
RAW = f"{volume_path(CFG)}/esci"
os.makedirs(RAW, exist_ok=True)
for name, (sha256, size) in ESCI_FILES.items():
    path = f"{RAW}/{name}"
    if not os.path.exists(path):
        with urllib.request.urlopen(esci_url(name)) as response, open(path + ".part", "wb") as out:
            shutil.copyfileobj(response, out, length=16 << 20)
        os.rename(path + ".part", path)
        print(f"downloaded {name}: {os.path.getsize(path) / 1e6:,.0f} MB")
    if os.path.getsize(path) != size or sha256_file(path) != sha256:
        raise RuntimeError(f"{path} doesn't match ESCI at the pinned commit; delete it to download again")
    print(f"{name}: sha256 ok")

# COMMAND ----------

# DBTITLE 1,Judgements: esci_labels
examples = (spark.read.parquet(f"{RAW}/shopping_queries_dataset_examples.parquet")
            .where(F.col("product_locale") == DATA["esci_locale"])
            .where(F.col("large_version") == 1))
if DATA["max_queries"]:
    # Deterministic subset: the same queries for the same seed, whatever the file order.
    subset = (examples.select("query_id").distinct()
              .orderBy(F.sha2(F.concat_ws("|", F.lit(DATA["sim"]["seed"]), F.col("query_id")), 256))
              .limit(DATA["max_queries"]))
    examples = examples.join(subset, "query_id")
gain = F.create_map(*[x for k, v in ESCI_GAIN.items() for x in (F.lit(k), F.lit(v))])
labels = examples.select("query_id", "product_id", "esci_label", gain[F.col("esci_label")].alias("gain"), "split")
labels.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table(CFG, "labels"))
labels = spark.table(table(CFG, "labels"))

# COMMAND ----------

# DBTITLE 1,Resumable ai_classify
def classify_resumably(name, rows, text_column, output):
    """Add `category` to `rows` via ai_classify into `output`, one checkpointed micro-batch per staging file."""
    staging, checkpoint = full_name(CFG, f"{name}_to_classify"), f"{CHECKPOINTS}/{name}"
    if RESET:
        spark.sql(f"DROP TABLE IF EXISTS {staging}")
        spark.sql(f"DROP TABLE IF EXISTS {output}")
        shutil.rmtree(checkpoint, ignore_errors=True)
    if spark.catalog.tableExists(output) and not os.path.exists(checkpoint):
        raise RuntimeError(f"{output} exists but has no checkpoint (written by an older run); "
                           "rerun with reset_classification=true")

    n_rows = rows.count()
    if not spark.catalog.tableExists(staging):
        # Small files on purpose: optimized writes / auto compaction would merge them into one big micro-batch.
        rows.limit(0).write.saveAsTable(staging)
        spark.sql(f"ALTER TABLE {staging} SET TBLPROPERTIES ("
                  "'delta.autoOptimize.optimizeWrite' = 'false', 'delta.autoOptimize.autoCompact' = 'false')")
        rows.repartition(max(1, math.ceil(n_rows / DATA["classify_batch_rows"]))).write.mode("append") \
            .saveAsTable(staging)
    elif spark.table(staging).count() != n_rows:
        raise RuntimeError(f"{staging} doesn't match this run's inputs; rerun with reset_classification=true")
    n_files = spark.sql(f"DESCRIBE DETAIL {staging}").first()["numFiles"]
    print(f"{name}: {n_rows:,} rows in {n_files} micro-batches")

    (spark.readStream.option("maxFilesPerTrigger", 1).table(staging)
     .repartition(DATA["classify_parallelism"])  # one file is one task; spread its LLM calls across tasks
     .selectExpr("*", f"ai_classify({text_column}, {CLASSIFY_ARGS}):response[0]::STRING AS category")
     .writeStream.trigger(availableNow=True).option("checkpointLocation", checkpoint)
     .toTable(output)
     .awaitTermination())
    return spark.table(output)


def categorize(name, rows, key, text_column, output, snapshot_file):
    """`rows` plus `category`, written to `output`: from the committed snapshot where it has the row (the same labels
    on every rebuild), ai_classify (resumable, into `<output>_llm`) for the rest. Export new labels with
    data/scripts/export_category_snapshot.py."""
    snapshot = (read_category_snapshot(f"{SNAPSHOTS}/{snapshot_file}", key, set(MEDIANS)) if USE_SNAPSHOT else [])
    known = spark.createDataFrame(snapshot, "_key string, category string")
    keyed = rows.withColumn("_key", F.col(key).cast("string"))
    hit = keyed.join(known, "_key").drop("_key")
    todo = keyed.join(known, "_key", "left_anti").drop("_key")
    n_todo = todo.count()
    parts = [hit]
    if n_todo:
        parts.append(classify_resumably(f"{name}_llm", todo, text_column, f"{output}_llm").select(*hit.columns))
    functools.reduce(DataFrame.unionByName, parts).write.mode("overwrite").option("overwriteSchema", "true") \
        .saveAsTable(output)
    print(f"{name}: {len(snapshot):,} snapshot labels, {n_todo:,} rows classified by ai_classify")
    return spark.table(output)

# COMMAND ----------

# DBTITLE 1,Queries: classify into categories
queries = categorize("queries", examples.select("query_id", "query", "split").dropDuplicates(["query_id"]),
                     "query_id", "query", table(CFG, "queries"), "query_categories.csv.gz")

# COMMAND ----------

# DBTITLE 1,Products: category by vote, title fallback, synthetic price
vote_udf = F.udf(lambda votes: category_vote([(v["category"], v["esci_label"]) for v in votes]), T.StringType())
voted = (labels.join(queries.select("query_id", "category"), "query_id")
         .where(F.col("category").isNotNull())
         .groupBy("product_id")
         .agg(F.collect_list(F.struct("category", "esci_label")).alias("votes"))
         .select("product_id", vote_udf("votes").alias("voted_category")))

products_raw = (spark.read.parquet(f"{RAW}/shopping_queries_dataset_products.parquet")
                .where(F.col("product_locale") == DATA["esci_locale"])
                .join(labels.select("product_id").distinct(), "product_id")
                .join(voted, "product_id", "left"))

# Only products with no E/S vote are classified from their title, so the LLM sees the smallest set.
title_categories = categorize(
    "product_titles",
    products_raw.where(F.col("voted_category").isNull())
    .select("product_id", F.substring(F.coalesce("product_title", F.lit("")), 1, 500).alias("title_text")),
    "product_id", "title_text", full_name(CFG, "product_title_categories"), "product_title_categories.csv.gz")
title_classified = (products_raw.where(F.col("voted_category").isNull())
                    .join(title_categories.select("product_id", "category"), "product_id", "left")
                    .withColumn("category_source", F.lit("title")))
voted_products = (products_raw.where(F.col("voted_category").isNotNull())
                  .withColumn("category", F.col("voted_category"))
                  .withColumn("category_source", F.lit("query_vote")))

medians, sigma, seed = MEDIANS, DATA["price_sigma"], DATA["sim"]["seed"]
price_udf = F.udf(lambda pid, category: synthetic_price(pid, medians[category], sigma, seed)
                  if category in medians else None, T.DoubleType())

products = (voted_products.unionByName(title_classified)
            .withColumn("price", price_udf("product_id", "category"))
            .select("product_id",
                    F.col("product_title").alias("title"),
                    F.col("product_description").alias("description"),
                    F.col("product_bullet_point").alias("bullet_points"),
                    F.col("product_brand").alias("brand"),
                    F.col("product_color").alias("color"),
                    "category", "category_source", "price"))
products.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table(CFG, "products"))
products = spark.table(table(CFG, "products"))

# COMMAND ----------

# DBTITLE 1,Summary
summary = {
    "queries": queries.count(),
    "queries_unclassified": queries.where("category IS NULL").count(),
    "labels": labels.count(),
    "products": products.count(),
    "products_by_category_source": {r["category_source"]: r["count"]
                                    for r in products.groupBy("category_source").count().collect()},
    "products_uncategorized": products.where("category IS NULL").count(),
    "products_without_brand": products.where("brand IS NULL OR brand = ''").count(),
}
by_category = (products.groupBy("category")
               .agg(F.count("*").alias("products"), F.expr("percentile(price, 0.5)").alias("median_price"))
               .orderBy(F.desc("products")))
display(by_category)
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
