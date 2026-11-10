# Databricks notebook source
# DBTITLE 1,SERP L2 training set: Feature View lookups -> model matrix, one row per search
# MAGIC %md
# MAGIC # SERP L2 training set: Feature View lookups → model matrix, one row per search
# MAGIC
# MAGIC 1. **Spine:** each sampled search (`serp_l2_searches`) × its L1 top 200 (`serp_l1_candidates`), with the
# MAGIC    product's price/brand/category from `products_search` (the index source) and a graded label from
# MAGIC    `serp_impressions`: cart 2, click 1, else 0. The last `models.serp_l2.valid_days` of train become `valid`.
# MAGIC 2. **`create_training_set(features=...)`:** session features from the Kafka `StreamSource`, user and item features
# MAGIC    from Delta, point-in-time at the search, plus the candidate fields as `RequestSource` features
# MAGIC    (`SERP_L2_REQUEST_COLUMNS`). `l1_rank`, `brand`, `category` and `is_guest` reuse the features QAC registered
# MAGIC    (a RequestSource feature only requires its own column); `l1_score` and `price` are new.
# MAGIC 3. **Model matrix** (`rts_ml.serp_l2.model_matrix`, the same function the logged model calls), packed per search
# MAGIC    in L1 order and padded to `models.serp_l2.candidates`: `x` (candidates × features, flattened), `label`, `mask`.
# MAGIC    Written to `serp_l2_matrix` with the split and the evaluation slices (first vs 2nd+ search, guest, intent).
# MAGIC
# MAGIC Searches are processed in hash buckets so no pandas frame holds all ~3M candidate rows. The `TrainingSet` itself
# MAGIC is rebuilt from `serp_l2_spine` by `serp_l2_dcnv2`, which needs it for `fe.log_model`.
# MAGIC
# MAGIC Checks: every search has exactly k candidates and the request columns; session, user and item features joined;
# MAGIC the matrix is finite; the share of searches whose engaged product is among the candidates (the L2 ceiling).
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.18.2" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys
from datetime import timedelta

import numpy as np
from pyspark.sql import functions as F
from pyspark.sql.window import Window

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.serp_l2 import COUNTS, MODEL_COLUMNS, PRICES, model_matrix, pack_group
from rts_shared.config import full_name, load_config, table
from rts_shared.features import feature_keys
from rts_shared.retrieval import SERP_INDEX_PK, SERP_L2_REQUEST_COLUMNS

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.feature_engineering.entities import ColumnSelection, RequestSource

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
UC, L2 = CFG["unity_catalog"], CFG["models"]["serp_l2"]
N = L2["candidates"]
SPINE, MATRIX = full_name(CFG, "serp_l2_spine"), full_name(CFG, "serp_l2_matrix")
BUCKETS = 8
fe = FeatureEngineeringClient()

# COMMAND ----------

# DBTITLE 1,Spine: searches x L1 top k, product fields, graded labels
searches = spark.table(table(CFG, "serp_l2_searches"))
cutoff = searches.where("split = 'train'").agg(F.max("event_time")).first()[0] - timedelta(days=L2["valid_days"])
searches = searches.withColumn("split", F.when((F.col("split") == "train") & (F.col("event_time") >= F.lit(cutoff)),
                                               "valid").otherwise(F.col("split")))

products = (spark.table(table(CFG, "products_search")).select(SERP_INDEX_PK, "price", "brand", "category")
            .withColumn("in_products", F.lit(True)))
labels = (spark.table(table(CFG, "serp_impressions")).where("clicked OR carted")
          .select("search_id", SERP_INDEX_PK, F.when(F.col("carted"), 2).otherwise(1).alias("label")))
candidates = (spark.table(table(CFG, "serp_l1_candidates")).where(F.col("rank") <= N)
              .select("query_id", F.col("rank").cast("int").alias("l1_rank"), SERP_INDEX_PK,
                      F.col("score").alias("l1_score")))

spine = (searches.join(candidates, "query_id").join(products, SERP_INDEX_PK, "left")
         .join(labels, ["search_id", SERP_INDEX_PK], "left")
         .select("search_id", "session_id", "user_id", "event_time", "split", SERP_INDEX_PK, "l1_rank", "l1_score",
                 F.col("price").cast("double").alias("price"), "brand", "category", "in_products",
                 F.col("user_id").isNull().cast("int").alias("is_guest"),
                 F.coalesce("label", F.lit(0)).cast("int").alias("label"),
                 F.abs(F.xxhash64("search_id") % BUCKETS).alias("bucket")))
spine.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(SPINE)
spine = spark.table(SPINE)

per_search = spine.groupBy("search_id").agg(F.count("*").alias("n"), F.max("label").alias("best"))
shape = per_search.agg(F.min("n").alias("min_n"), F.max("n").alias("max_n"),
                       F.avg((F.col("best") > 0).cast("double")).alias("engaged_in_candidates")).first().asDict()
gaps = spine.agg(F.count_if(F.col("in_products").isNull()).alias("missing_product"),
                 F.count_if(F.col("price").isNull()).alias("null_price")).first().asDict()
# A few indexed products have no category, so no synthetic price; the model reads that as missing, as at serving.
print(shape, gaps)
assert shape["min_n"] == shape["max_n"] == N, f"searches should have exactly {N} candidates: {shape}"
assert gaps["missing_product"] == 0, f"{gaps['missing_product']} candidates missing from products_search"

# COMMAND ----------

# DBTITLE 1,Features: lookups + candidate RequestSource features
request_source = RequestSource.from_dataframe(dataframe=spine.select(*SERP_L2_REQUEST_COLUMNS))


def candidate_feature(column):
    # Named after its request column: a RequestSource feature's value is that column of the request.
    try:
        return fe.create_feature(source=request_source, function=ColumnSelection(column),
                                 catalog_name=UC["catalog"], schema_name=UC["schema"], name=column)
    except Exception as error:  # definitions persist in UC across runs, and QAC registered some of these
        if "already exists" not in str(error).lower():
            raise
        return fe.get_feature(full_name=full_name(CFG, column))


LOOKUP_FEATURES = list(feature_keys(CFG))  # session, user and item
FEATURES = ([fe.get_feature(full_name=full_name(CFG, n)) for n in LOOKUP_FEATURES]
            + [candidate_feature(c) for c in SERP_L2_REQUEST_COLUMNS])
# Extra spine columns (split, bucket, keys) pass through to load_df.
training_set = fe.create_training_set(df=spine, features=FEATURES, label="label", exclude_columns=[])
joined = training_set.load_df()

# Every raw column model_matrix reads.
needed = {*COUNTS, *PRICES, "sess_std_price_30m", "sess_last_category", "sess_last_brand",
          "sess_cat_cnt_30m", "user_cat_cnt_30d", *SERP_L2_REQUEST_COLUMNS}
assert needed <= set(joined.columns), f"training set lacks {sorted(needed - set(joined.columns))}"
print(dict(joined.dtypes))

# COMMAND ----------

# DBTITLE 1,Slices for evaluation (never features)
nth = F.row_number().over(Window.partitionBy("session_id").orderBy("event_time"))
search_index = (spark.table(table(CFG, "qac_impressions")).withColumn("search_index", nth)
                .select("search_id", F.when(F.col("search_index") == 1, "first").otherwise("2nd+").alias("search")))
intent = spark.table(table(CFG, "sim_sessions")).select(
    "session_id", F.when(F.col("intent_category").isNull(), "none")
    .when(F.col("intent_brand").isNull(), "category").otherwise("category+brand").alias("intent"))
slices = (searches.join(search_index, "search_id").join(intent, "session_id", "left")
          .select("search_id", "split", "search", "intent",
                  F.when(F.col("user_id").isNull(), "guest").otherwise("logged-in").alias("guest"))
          .toPandas().set_index("search_id"))

# COMMAND ----------

# DBTITLE 1,Model matrix, packed per search (bucket by bucket)
COLUMNS = list(MODEL_COLUMNS)
SCHEMA = ("search_id string, split string, search string, guest string, intent string, "
          "x array<float>, label array<float>, mask array<float>")
spark.sql(f"DROP TABLE IF EXISTS {MATRIX}")
coverage, finite = [], True
for b in range(BUCKETS):
    pdf = joined.where(F.col("bucket") == b).toPandas()
    X = model_matrix(pdf)
    finite &= bool(np.isfinite(X.to_numpy()).all())
    # Real history, not just non-null: counts are 0 (not null) without history.
    guest = pdf["is_guest"] == 1
    coverage.append({"rows": len(pdf), "earlier_search": int((pdf["sess_n_searches_30m"].fillna(0) > 0).sum()),
                     "session_engaged": int(pdf["sess_last_category"].notna().sum()),
                     "user_history": int((pdf["user_n_clicks_30d"].fillna(0) > 0).sum()),
                     "guest_user_history": int((guest & (pdf["user_n_clicks_30d"].fillna(0) > 0)).sum()),
                     "item_history": int((pdf["item_clicks_7d"].fillna(0) > 0).sum()),
                     "brand": int(pdf["brand"].notna().sum())})
    X = X.assign(search_id=pdf["search_id"].values, l1_rank=pdf["l1_rank"].values, label=pdf["label"].values)
    out = []
    for search_id, g in X.groupby("search_id"):
        x, y, mask = pack_group(g, COLUMNS, n=N)
        s = slices.loc[search_id]
        out.append((search_id, s["split"], s["search"], s["guest"], s["intent"], x.ravel().tolist(), y.tolist(),
                    mask.tolist()))
    spark.createDataFrame(out, SCHEMA).write.mode("append").saveAsTable(MATRIX)
    print(f"bucket {b}: {len(pdf):,} rows, {len(out):,} searches")
    del pdf, X, out

# COMMAND ----------

# DBTITLE 1,Checks and summary
rows = sum(c["rows"] for c in coverage)
share = {k: round(sum(c[k] for c in coverage) / rows, 4) for k in coverage[0] if k != "rows"}  # of all rows
matrix = spark.table(MATRIX)
counts = {r["split"]: r["count"] for r in matrix.groupBy("split").count().collect()}
summary = {"candidates_per_search": N, "features": len(COLUMNS), "spine_rows": rows, "searches_by_split": counts,
           "engaged_product_in_candidates": round(shape["engaged_in_candidates"], 4), "null_price_rows": gaps["null_price"],
           "history_share": share, "valid_cutoff": str(cutoff), "matrix_finite": finite}
print(json.dumps(summary, indent=2))
assert finite, "the model matrix has NaN or inf values"
assert sum(counts.values()) == per_search.count(), "some searches were not packed"
assert set(counts) == {"train", "valid", "test"}, f"splits: {counts}"
assert share["earlier_search"] > 0 and share["session_engaged"] > 0 and share["user_history"] > 0 \
    and share["item_history"] > 0, f"session, user or item features did not join: {share}"
assert share["guest_user_history"] == 0, "guest rows have user history: user features joined on a null key?"
dbutils.notebook.exit(json.dumps(summary))
