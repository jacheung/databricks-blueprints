# Databricks notebook source
# MAGIC %md
# MAGIC # Path 3 · Save models → online (real-time) inference
# MAGIC
# MAGIC Use this when an app needs one group's forecast on demand. Same saved models and router as
# MAGIC path 2, plus a Lakebase online store and a Model Serving endpoint.
# MAGIC
# MAGIC 1. Fit one Prophet model per group and store it as slim Prophet JSON (~8 KB) in a feature
# MAGIC    table keyed by `group_name`.
# MAGIC 2. Publish the feature table to a Lakebase online store.
# MAGIC 3. Log one **router** model with `fe.log_model`. It holds no per-group weights; its feature
# MAGIC    lineage says "`model_json` comes from `prophet_models`, keyed by `group_name`".
# MAGIC 4. Serve the router. A request carries only `group_name` and `horizon`; the endpoint looks up
# MAGIC    that group's `model_json` in Lakebase before calling the router.
# MAGIC
# MAGIC The online store and endpoint bill while they exist (no scale-to-zero); the last cell deletes
# MAGIC them. Run `00_generate_timeseries_data` first.

# COMMAND ----------

# MAGIC %pip install -q prophet==1.5.0 mlflow==3.18.0 databricks-feature-engineering==0.18.3 databricks-sdk==0.167.0

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "scaling_forecasting")
dbutils.widgets.text("n_groups", "10")
dbutils.widgets.dropdown("skew", "false", ["false", "true"])
dbutils.widgets.text("horizon", "14")
dbutils.widgets.text("online_store", "scaling-forecasting-online-store")
dbutils.widgets.text("endpoint_name", "scaling-forecasting-router")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
horizon = int(dbutils.widgets.get("horizon"))
online_store_name = dbutils.widgets.get("online_store")
endpoint_name = dbutils.widgets.get("endpoint_name")
models_table = f"{catalog}.{schema}.prophet_models"
online_table = f"{catalog}.{schema}.prophet_models_online"
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
# MAGIC The feature table's primary key is `group_name`, and its change data feed lets the online
# MAGIC store sync only changed rows. Retraining upserts rows (`mode="merge"`), so the router logged
# MAGIC below doesn't need a new version when models change.

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

# MAGIC %md ## 2. Publish to the Lakebase online store

# COMMAND ----------

import time

store = fe.get_online_store(name=online_store_name)  # returns None if absent
if store is None:
    fe.create_online_store(name=online_store_name, capacity="CU_1")
for _ in range(60):
    store = fe.get_online_store(name=online_store_name)
    if str(store.state).endswith("AVAILABLE"):
        break
    time.sleep(20)
print(online_store_name, store.state)

fe.publish_table(online_store=store, source_table_name=models_table,
                 online_table_name=online_table, publish_mode="TRIGGERED")

# COMMAND ----------

# MAGIC %md ## 3. Log one router model with feature lineage

# COMMAND ----------

import mlflow  # noqa: E402
import prophet  # noqa: E402
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
        extra_pip_requirements=[f"prophet=={prophet.__version__}"],
    )

client = mlflow.MlflowClient()
version = max(int(v.version) for v in client.search_model_versions(f"name='{model_name}'"))
client.set_registered_model_alias(model_name, "prod", version)
print(model_name, "version", version, "→ @prod")

# COMMAND ----------

# MAGIC %md ## 4. Serve the router and query one group

# COMMAND ----------

from datetime import timedelta  # noqa: E402

from databricks.sdk import WorkspaceClient  # noqa: E402
from databricks.sdk.service.serving import EndpointCoreConfigInput, ServedEntityInput  # noqa: E402

w = WorkspaceClient()
served = [ServedEntityInput(entity_name=model_name, entity_version=str(version),
                            workload_size="Small", scale_to_zero_enabled=False)]
if any(e.name == endpoint_name for e in w.serving_endpoints.list()):
    w.serving_endpoints.update_config_and_wait(endpoint_name, served_entities=served, timeout=timedelta(minutes=45))
else:
    w.serving_endpoints.create_and_wait(endpoint_name,
                                        config=EndpointCoreConfigInput(name=endpoint_name, served_entities=served),
                                        timeout=timedelta(minutes=45))

# The request has no model_json: the endpoint fetches it from Lakebase by group_name.
group = keys.first()["group_name"]
online = w.serving_endpoints.query(endpoint_name,
                                   dataframe_records=[{"group_name": group, "horizon": horizon}]).predictions[0]

# Check against a forecast computed directly from the stored model.
stored = spark.table(models_table).filter(F.col("group_name") == group).first()["model_json"]
expected = forecasting.forecast_from_json(stored, horizon)
assert online["ds"] == expected["ds"], "endpoint and stored model forecast different timestamps"
assert all(abs(a - b) < 1e-6 for a, b in zip(online["yhat"], expected["yhat"])), "yhat differs"
print(f"{group}: endpoint forecast matches the stored model on {len(online['yhat'])} points")

# COMMAND ----------

# MAGIC %md ## 5. Clean up (optional)
# MAGIC
# MAGIC The online store and endpoint bill while they exist (no scale-to-zero). Uncomment and run
# MAGIC this cell to delete them. The `prophet_models` table and the registered router stay, so
# MAGIC path 2 keeps working and rerunning this notebook brings path 3 back.

# COMMAND ----------

# from databricks.sdk.errors import NotFound
#
# for label, delete in [  # endpoint first, then the online table, then the store that holds it
#     (f"endpoint {endpoint_name}", lambda: w.serving_endpoints.delete(endpoint_name)),
#     (f"online table {online_table}", lambda: w.database.delete_synced_database_table(online_table)),
#     (f"online store {online_store_name}", lambda: fe.delete_online_store(name=online_store_name)),
# ]:
#     try:
#         delete()
#         print("deleted", label)
#     except NotFound:
#         print("absent ", label)
