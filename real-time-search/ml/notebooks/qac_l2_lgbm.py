# Databricks notebook source
# DBTITLE 1,QAC L2: session-aware LightGBM reranker
# MAGIC %md
# MAGIC # QAC L2: session-aware LightGBM reranker
# MAGIC
# MAGIC Reranks the L1 completions for a typed prefix using what the session (and the user) did before the keystroke:
# MAGIC
# MAGIC 1. **Spine:** sampled searches x typed prefixes (`models.qac_l2.prefix_lens`) x the L1 top
# MAGIC    `models.qac_l2.candidates` for that prefix from `qac_prefix_completions`. Label = the candidate is the
# MAGIC    completion the user submitted. One ranking group per (search, prefix).
# MAGIC 2. **Training set** with `create_training_set(features=...)`: session features from the Kafka `StreamSource`,
# MAGIC    user features from Delta, both point-in-time at the search, and the candidate fields as `RequestSource`
# MAGIC    features (the L1 row the app already holds, plus `is_guest`). Serving supports only models whose inputs are all
# MAGIC    Feature View features, so the request fields are declared too, and it strips the lookup keys before the model.
# MAGIC 3. **LambdaRank** (`rts_ml.qac_l2`): crosses (candidate vs session/user) inside the model, no brand or
# MAGIC    category identities. Train on the train split minus its last `valid_days`, which early-stop.
# MAGIC 4. **L1 vs L2 on the test split**, MRR@5 and success@1 with paired bootstrap 95% CIs, overall and per slice.
# MAGIC    The go/no-go for the endpoint is read from this table.
# MAGIC 5. **`fe.log_model`** as a pyfunc (`rts_ml.qac_l2_model.QacRanker`) registered to `models.qac_l2.registered_name`.
# MAGIC
# MAGIC Checks: at most one positive per group, the session and user features joined, and the registered model
# MAGIC reloads and scores the test rows exactly as in memory. Lift is reported, not asserted.
# MAGIC
# MAGIC Known: L1 popularity counts the train searches trained on here (mild leakage); the simulator's intent never
# MAGIC drifts within a session, so lift is optimistic vs production.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.18.2" lightgbm scikit-learn pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import posixpath
import sys

import lightgbm
import mlflow
import pandas as pd
import sklearn
from pyspark.sql import functions as F
from pyspark.sql.window import Window

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.qac_l2 import CROSSES, MODEL_COLUMNS, fit_ranker, score, slice_report
from rts_ml.qac_l2_model import QacRanker
from rts_shared.config import full_name, load_config, model_name, resolve_auto, table
from rts_shared.features import feature_keys
from rts_shared.retrieval import QAC_L2_CANDIDATE_COLUMNS, QAC_L2_REQUEST_COLUMNS, normalize

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.feature_engineering.entities import ColumnSelection, RequestSource
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
w = WorkspaceClient()
CFG = resolve_auto(load_config(dbutils.widgets.get("config_path")), w.current_user.me().user_name)
UC, L2 = CFG["unity_catalog"], CFG["models"]["qac_l2"]
MODEL_NAME = model_name(CFG, "qac_l2")
fe = FeatureEngineeringClient()

# Session and user features the model reads (item features are SERP's).
LOOKUP_FEATURES = [n for n in feature_keys(CFG) if not n.startswith("item_")]
missing = set(MODEL_COLUMNS) - set(LOOKUP_FEATURES) - set(QAC_L2_REQUEST_COLUMNS) - set(CROSSES)
assert not missing, f"model columns not provided by any feature (window suffixes vs config?): {missing}"

# COMMAND ----------

# DBTITLE 1,Spine: searches x typed prefixes x L1 candidates
impressions = spark.table(table(CFG, "qac_impressions"))
# Normalize on the driver with the same function as the L1 table, so prefixes match its keys.
queries = [r["query"] for r in impressions.select("query").distinct().collect()]
completion_of = spark.createDataFrame([(q, normalize(q)) for q in queries], "query string, submitted string")
nth_search = F.row_number().over(Window.partitionBy("session_id").orderBy("event_time"))
searches = (impressions.withColumn("search_index", nth_search).join(completion_of, "query")
            .where("submitted != ''")
            .select("search_id", "session_id", "user_id", "event_time", "split", "search_index", "submitted"))


def sample(split, n):
    return searches.where(F.col("split") == split).orderBy(F.sha2(F.col("search_id"), 256)).limit(n)


l1 = (spark.table(table(CFG, "qac_prefix_completions")).where(F.col("rank") <= L2["candidates"])
      .select("prefix", F.col("rank").alias("l1_rank"), "completion",
              *[c for c in QAC_L2_CANDIDATE_COLUMNS if c != "l1_rank"]))


def spine(df):
    typed = (df.withColumn("n", F.explode(F.array(*[F.lit(n) for n in L2["prefix_lens"]])))
             .where(F.length("submitted") >= F.col("n"))
             .withColumn("prefix", F.expr("substring(submitted, 1, n)")))
    return (typed.join(l1, "prefix")
            .select(F.concat_ws(":", "search_id", F.col("n").cast("string")).alias("group_id"), "search_id",
                    "session_id", "user_id", "event_time", *QAC_L2_CANDIDATE_COLUMNS,
                    F.col("user_id").isNull().cast("int").alias("is_guest"),
                    (F.col("completion") == F.col("submitted")).cast("int").alias("label")))


train_searches, test_searches = sample("train", L2["train_searches"]), sample("test", L2["eval_searches"])
train_spine, test_spine = spine(train_searches), spine(test_searches)
max_positives = train_spine.unionByName(test_spine).groupBy("group_id").agg(F.sum("label").alias("p")) \
    .agg(F.max("p")).first()[0]
assert max_positives == 1, f"a ranking group has {max_positives} positives; completions should be unique per prefix"

# COMMAND ----------

# DBTITLE 1,Features: lookups + candidate RequestSource features
request_source = RequestSource.from_dataframe(dataframe=train_spine.select(*QAC_L2_REQUEST_COLUMNS))


def candidate_feature(column):
    # Named after its request column: a RequestSource feature's value is that column of the request.
    try:
        return fe.create_feature(source=request_source, function=ColumnSelection(column),
                                 catalog_name=UC["catalog"], schema_name=UC["schema"], name=column)
    except Exception as error:  # definitions persist in UC across runs
        if "already exists" not in str(error).lower():
            raise
        return fe.get_feature(full_name=full_name(CFG, column))


FEATURES = ([fe.get_feature(full_name=full_name(CFG, n)) for n in LOOKUP_FEATURES]
            + [candidate_feature(c) for c in QAC_L2_REQUEST_COLUMNS])


def training_set(df):
    return fe.create_training_set(df=df, features=FEATURES, label="label", exclude_columns=[])


train_set = training_set(train_spine)
train_pdf = train_set.load_df().toPandas()
test_pdf = training_set(test_spine).load_df().toPandas()

coverage = {c: float(train_pdf[c].notna().mean()) for c in ("sess_last_category", "sess_n_searches_30m",
                                                               "user_n_clicks_30d", "brand")}
print("non-null share in train:", coverage)
assert coverage["sess_last_category"] > 0 and coverage["user_n_clicks_30d"] > 0, \
    f"session or user features did not join: {coverage}"

# COMMAND ----------

# DBTITLE 1,Train: LambdaRank, early-stopped on the last train days
cutoff = train_pdf["event_time"].max() - pd.Timedelta(days=L2["valid_days"])
with_positive = train_pdf.groupby("group_id")["label"].transform("max") == 1  # groups L1 missed teach nothing
fit_rows, valid_rows = train_pdf[with_positive & (train_pdf["event_time"] < cutoff)], \
    train_pdf[with_positive & (train_pdf["event_time"] >= cutoff)]
ranker = fit_ranker(fit_rows, valid_rows)
print(f"{fit_rows['group_id'].nunique()} train / {valid_rows['group_id'].nunique()} valid groups; "
      f"best iteration {ranker.best_iteration_}")

# COMMAND ----------

# DBTITLE 1,Evaluate: L1 vs L2 on the test split, overall and per slice
intent = spark.table(table(CFG, "sim_sessions")).select(
    "session_id", F.when(F.col("intent_category").isNull(), "none")
    .when(F.col("intent_brand").isNull(), "category").otherwise("category+brand").alias("intent"))
slices = (test_searches.join(intent, "session_id", "left")
          .select("search_id", F.when(F.col("search_index") == 1, "first").otherwise("2nd+").alias("search"),
                  "intent").toPandas())
test_pdf = test_pdf.merge(slices, on="search_id")
test_pdf["guest"] = test_pdf["user_id"].isna().map({True: "guest", False: "logged-in"})
test_pdf["l1"], test_pdf["l2"] = -test_pdf["l1_rank"], score(ranker, test_pdf)

# JSON round trip: slice values come back as numpy scalars, which MLflow and the exit payload can't serialize.
report = json.loads(json.dumps(slice_report(test_pdf, ["search", "guest", "prefix_len", "intent"]),
                               default=lambda o: o.item()))
report_df = pd.DataFrame(report).astype({"value": str})  # slice values mix strings and prefix lengths
display(report_df)

n_groups = test_spine.select("group_id").distinct().count()
typed_groups = test_searches.select(F.explode(F.array(*[F.lit(n) for n in L2["prefix_lens"]])).alias("n"),
                                    "submitted").where(F.length("submitted") >= F.col("n")).count()
ceiling = {"groups": typed_groups, "with_candidates": n_groups,
           "submitted_in_l1_top_k": int(test_pdf.groupby("group_id")["label"].max().sum())}
ceiling["share"] = ceiling["submitted_in_l1_top_k"] / ceiling["groups"]
print("ceiling (submitted query among the L1 candidates):", ceiling)

# COMMAND ----------

# DBTITLE 1,Log and register (fe.log_model, pyfunc)
experiment = CFG["mlflow"]["experiment"]
w.workspace.mkdirs(posixpath.dirname(experiment))
mlflow.set_experiment(experiment)
mlflow.set_registry_uri("databricks-uc")
overall = {r["metric"]: r for r in report if r["slice"] == "all"}

with mlflow.start_run(run_name="qac_l2_lgbm") as run:
    mlflow.log_params({**{k: L2[k] for k in ("train_searches", "valid_days", "candidates")},
                       "prefix_lens": L2["prefix_lens"], "best_iteration": ranker.best_iteration_,
                       **{f"lgbm_{k}": v for k, v in ranker.get_params().items() if v is not None}})
    mlflow.log_metrics({f"test_{m}_{side}": overall[m][side] for m in ("mrr", "success1")
                        for side in ("l1", "l2", "lift")})
    mlflow.log_dict({"report": report, "ceiling": ceiling}, "eval/report.json")
    fe.log_model(model=QacRanker(ranker), artifact_path="model", flavor=mlflow.pyfunc, training_set=train_set,
                 registered_model_name=MODEL_NAME,
                 code_paths=[f"{ROOT}/ml/src/rts_ml", f"{ROOT}/shared/src/rts_shared"],
                 extra_pip_requirements=[f"lightgbm=={lightgbm.__version__}",
                                         f"scikit-learn=={sklearn.__version__}"])

version = max(int(v.version) for v in mlflow.MlflowClient().search_model_versions(f"name='{MODEL_NAME}'")
              if v.run_id == run.info.run_id)
print(f"{MODEL_NAME} version {version} (run {run.info.run_id})")

# COMMAND ----------

# DBTITLE 1,Check: the registered model scores like the one in memory
# Loads the pyfunc (crosses + LightGBM, from code_paths) and scores the already-joined test rows without the lookup
# keys: the endpoint strips them before calling the model.
loaded = mlflow.pyfunc.load_model(f"runs:/{run.info.run_id}/model/data/feature_store/raw_model")
check = test_pdf.head(2000).drop(columns=["session_id", "user_id"])
diff = float(abs(pd.Series(loaded.predict(check)) - pd.Series(score(ranker, check))).max())
assert diff < 1e-9, f"reloaded model scores differ by up to {diff}"

# COMMAND ----------

# DBTITLE 1,Summary
summary = {
    "model": MODEL_NAME, "version": version, "run_id": run.info.run_id,
    "train_groups": int(fit_rows["group_id"].nunique()), "valid_groups": int(valid_rows["group_id"].nunique()),
    "test_groups": int(test_pdf["group_id"].nunique()), "best_iteration": ranker.best_iteration_,
    "coverage": coverage, "ceiling": ceiling,
    "overall": {m: {k: round(overall[m][k], 4) for k in ("l1", "l2", "lift", "lift_lo", "lift_hi")}
                for m in overall},
    "slices": [{k: (round(v, 4) if isinstance(v, float) else v) for k, v in r.items()} for r in report],
}
print(json.dumps(summary, indent=2, default=str))
dbutils.notebook.exit(json.dumps(summary, default=str))
