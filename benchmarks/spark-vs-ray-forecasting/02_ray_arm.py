# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Ray arm: Ray Data `map_groups`
# MAGIC
# MAGIC Times per-group Prophet **train + forecast** only. Ray reads the input through a SQL
# MAGIC warehouse (`read_databricks_tables`) before the clock starts. Cluster setup follows
# MAGIC `jacheung/ray-on-databricks-rct/parallelize-prophet-forecasting`: 10 workers, Ray takes 8
# MAGIC (128 CPUs, the same as the Spark arm; the driver hosts the Ray head with 0 CPUs) and
# MAGIC Spark keeps the other 2 for writing results back to Delta.

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "jon_cheung")
dbutils.widgets.text("n_groups", "1000")
dbutils.widgets.dropdown("skew", "false", ["false", "true"])
dbutils.widgets.text("horizon", "14")
dbutils.widgets.text("partitions", "0")  # 0 = one per group
dbutils.widgets.text("warehouse_id", "")
dbutils.widgets.text("ray_worker_nodes", "8")
dbutils.widgets.text("cpus_per_node", "16")

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
n_groups = int(dbutils.widgets.get("n_groups"))
skew = dbutils.widgets.get("skew") == "true"
horizon = int(dbutils.widgets.get("horizon"))
warehouse_id = dbutils.widgets.get("warehouse_id")
ray_worker_nodes = int(dbutils.widgets.get("ray_worker_nodes"))
cpus_per_node = int(dbutils.widgets.get("cpus_per_node"))

# COMMAND ----------

import os
import sys
import time
from functools import partial

import cloudpickle
import ray
import ray.cloudpickle
from ray.util.spark import setup_ray_cluster, shutdown_ray_cluster

sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
import bench  # noqa: E402
import forecasting  # noqa: E402

partitions = bench.resolve_partitions(int(dbutils.widgets.get("partitions")), n_groups)

cloudpickle.register_pickle_by_value(forecasting)
ray.cloudpickle.register_pickle_by_value(forecasting)  # Ray ships tasks with its own cloudpickle

table_suffix = forecasting.source_table_name(n_groups, skew)
source = f"{catalog}.{schema}.{table_suffix}"

# COMMAND ----------

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import ResourceAlreadyExists
from databricks.sdk.service.catalog import VolumeType

# Volumes for Ray's temp files, collected Ray logs, and the Parquet handoff to Spark (rct pattern).
w = WorkspaceClient()
for volume in ("ray_data_tmp_dir", "ray_collected_logs", "ray_out"):
    try:
        w.volumes.create(catalog_name=catalog, schema_name=schema, name=volume, volume_type=VolumeType.MANAGED)
    except ResourceAlreadyExists:
        pass
os.environ["RAY_UC_VOLUMES_FUSE_TEMP_DIR"] = f"/Volumes/{catalog}/{schema}/ray_data_tmp_dir/"

# Restart cleanly if a previous Ray-on-Spark cluster is still attached to this Spark cluster.
for stop in (shutdown_ray_cluster, ray.shutdown):
    try:
        stop()
    except Exception:
        pass

# Ray on `ray_worker_nodes` workers only; the remaining Spark workers are left alone for the write.
setup_ray_cluster(
    min_worker_nodes=ray_worker_nodes,
    max_worker_nodes=ray_worker_nodes,
    num_cpus_worker_node=cpus_per_node,
    num_gpus_worker_node=0,
    num_cpus_head_node=0,  # head on the driver, no tasks there: Ray CPUs == Spark arm's task slots
    collect_log_to_path=f"/Volumes/{catalog}/{schema}/ray_collected_logs",
)
ray.init()  # Ray 2.60's setup_ray_cluster sets RAY_ADDRESS but doesn't connect the driver
cores = int(ray.cluster_resources()["CPU"])
print("Ray CPUs:", cores)

# COMMAND ----------

# MAGIC %md ## Untimed: read through the SQL warehouse
# MAGIC
# MAGIC Read tasks run on Ray workers, which have no notebook auth context, so the notebook's
# MAGIC own short-lived credential is resolved here on the driver and handed to Ray.

# COMMAND ----------

from ray.data._internal.datasource.databricks_credentials import StaticCredentialProvider

cfg = w.config
token = cfg.authenticate()["Authorization"].removeprefix("Bearer ")
creds = StaticCredentialProvider(token=token, host=cfg.host)

ds = ray.data.read_databricks_tables(warehouse_id=warehouse_id, table=source, credential_provider=creds).materialize()
print(f"{source}: {ds.count():,} rows, {cores} CPUs, {partitions} partitions")

# COMMAND ----------

# MAGIC %md ## Timed: group → fit → forecast

# COMMAND ----------

t0 = time.perf_counter()
out = (ds.groupby("group_name", num_partitions=partitions)
         .map_groups(partial(forecasting.train_and_forecast, horizon=horizon), batch_format="pandas", num_cpus=1)
         .materialize())
wall_s = time.perf_counter() - t0
print(f"train + forecast: {wall_s:.1f}s")

# COMMAND ----------

# MAGIC %md ## Untimed: record the result; write forecasts via Parquet on a Volume → Delta (Spark nodes)

# COMMAND ----------

row = bench.result_row(engine="ray", n_groups=n_groups, skew=skew, cores=cores, partitions=partitions,
                       wall_s=wall_s, forecasts=out.select_columns(["group_name", "fit_s", "predict_s"]).to_pandas())
spark.createDataFrame([row], bench.RESULT_SCHEMA).write.mode("append").saveAsTable(f"{catalog}.{schema}.benchmark_results")

parquet_dir = f"/Volumes/{catalog}/{schema}/ray_out/forecasts_{table_suffix}"
dbutils.fs.rm(parquet_dir, recurse=True)
out.write_parquet(parquet_dir)
spark.read.parquet(parquet_dir).write.mode("overwrite").saveAsTable(f"{catalog}.{schema}.forecasts_ray_{table_suffix}")
display(spark.createDataFrame([row], bench.RESULT_SCHEMA))

# COMMAND ----------

shutdown_ray_cluster()
