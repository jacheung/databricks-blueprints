# Databricks notebook source
# MAGIC %md
# MAGIC # Path 2 · Save models → offline (batch) inference
# MAGIC
# MAGIC Use this when you forecast more often than you retrain, or want models you can audit and
# MAGIC roll back, and batch forecasts are enough. Runs on serverless; no Lakebase or endpoint.
# MAGIC
# MAGIC 1. Fit one Prophet model per group and store it as slim Prophet JSON (~8 KB) in a feature
# MAGIC    table keyed by `group_name`.
# MAGIC 2. Log one **router** model with `fe.log_model`. It holds no per-group weights; its feature
# MAGIC    lineage says "`model_json` comes from `prophet_models`, keyed by `group_name`".
# MAGIC 3. `fe.score_batch` with keys only: Feature Engineering joins each group's `model_json` from
# MAGIC    the Delta table and the router forecasts from it.
# MAGIC
# MAGIC Run `00_generate_timeseries_data` first.

# COMMAND ----------

# MAGIC %pip install -q -r lib/requirements.txt mlflow==3.18.0 databricks-feature-engineering==0.18.3

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "scaling_forecasting")
dbutils.widgets.text("n_groups", "10")
dbutils.widgets.dropdown("skew", "false", ["false", "true"])
dbutils.widgets.text("horizon", "14")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
horizon = int(dbutils.widgets.get("horizon"))
models_table = f"{catalog}.{schema}.prophet_models"
model_name = f"{catalog}.{schema}.prophet_router"

# COMMAND ----------

import os
import sys

import cloudpickle

lib_dir = os.path.join(os.getcwd(), "lib")
sys.path.insert(0, lib_dir)
import forecasting  # noqa: E402

cloudpickle.register_pickle_by_value(forecasting)
n_groups = int(dbutils.widgets.get("n_groups"))  # partition count; see 01_no_save_forecast for how to choose it
source_table = f"{catalog}.{schema}.{forecasting.source_table_name(n_groups, dbutils.widgets.get('skew') == 'true')}"

# COMMAND ----------

# MAGIC %md ## 1. Train: one model per group → slim JSON → `prophet_models` feature table
# MAGIC
# MAGIC The feature table's primary key is `group_name`. Retraining upserts rows (`mode="merge"`), so
# MAGIC the router logged below doesn't need a new version when models change.
# MAGIC
# MAGIC Paths 2 and 3 both write `prophet_models`. Don't run them at the same time on the same schema:
# MAGIC their merges conflict (`ConcurrentAppendException`). Run one after the other, or use separate schemas.

# COMMAND ----------

from databricks.feature_engineering import FeatureEngineeringClient

fe = FeatureEngineeringClient()

models = (spark.read.table(source_table)
               .repartition(n_groups, "group_name")
               .groupBy("group_name")
               .applyInPandas(forecasting.train_to_json, forecasting.MODEL_SCHEMA))

if not spark.catalog.tableExists(models_table):
    fe.create_table(name=models_table, primary_keys=["group_name"], schema=models.schema,
                    description="One slim Prophet JSON model per group_name.")
    spark.sql(f"ALTER TABLE {models_table} SET TBLPROPERTIES ('delta.enableChangeDataFeed' = 'true')")
fe.write_table(name=models_table, df=models, mode="merge")

display(spark.sql(f"""SELECT count(*) AS models, avg(json_bytes) AS avg_json_bytes, max(json_bytes) AS max_json_bytes
                      FROM {models_table}"""))

# COMMAND ----------

# MAGIC %md ## 2. Log one router model with feature lineage

# COMMAND ----------

import mlflow  # noqa: E402
from databricks.feature_engineering import FeatureLookup  # noqa: E402
from pyspark.sql import functions as F  # noqa: E402

from router_model import OUTPUT_SCHEMA, ProphetRouter  # noqa: E402

mlflow.set_registry_uri("databricks-uc")

keys = spark.table(models_table).select("group_name").withColumn("horizon", F.lit(horizon))
training_set = fe.create_training_set(
    df=keys,
    feature_lookups=[FeatureLookup(table_name=models_table, lookup_key="group_name", feature_names=["model_json"])],
    label=None,
)

with mlflow.start_run(run_name="prophet_router"):
    fe.log_model(
        model=ProphetRouter(),
        artifact_path="model",
        flavor=mlflow.pyfunc,
        training_set=training_set,
        output_schema=OUTPUT_SCHEMA,  # UC needs one; FE can't infer it without a label
        registered_model_name=model_name,
        code_paths=[os.path.join(lib_dir, "forecasting.py"), os.path.join(lib_dir, "router_model.py")],
        extra_pip_requirements=open(os.path.join(lib_dir, "requirements.txt")).read().split(),
    )

client = mlflow.MlflowClient()
version = max(int(v.version) for v in client.search_model_versions(f"name='{model_name}'"))
client.set_registered_model_alias(model_name, "prod", version)
print(model_name, "version", version, "→ @prod")

# COMMAND ----------

# MAGIC %md ## 3. Batch forecasts: `fe.score_batch` with keys only

# COMMAND ----------

result_type = ("struct<group_name:string, ds:array<string>, yhat:array<double>, "
               "yhat_lower:array<double>, yhat_upper:array<double>>")
scored = fe.score_batch(model_uri=f"models:/{model_name}@prod", df=keys, result_type=result_type)
batch = scored.select("prediction.*")  # the struct carries group_name

n_keys, n_scored = keys.count(), batch.count()
assert n_scored == n_keys, f"score_batch returned {n_scored} forecasts for {n_keys} keys"
assert batch.filter(F.size("yhat") != horizon).isEmpty(), f"some forecasts don't have {horizon} points"
print(f"score_batch: {n_scored:,} groups × {horizon} points")
display(batch)
