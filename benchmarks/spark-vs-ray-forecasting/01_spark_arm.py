# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Spark arm: `applyInPandas`
# MAGIC
# MAGIC Times per-group Prophet **train + forecast** only. The input is read and cached
# MAGIC before the clock starts; the forecasts are written to Delta after it stops.
# MAGIC Run on an 8-worker `m5.4xlarge` cluster (128 task slots) to match the Ray arm's 128 CPUs.

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "jon_cheung")
dbutils.widgets.text("n_groups", "1000")
dbutils.widgets.dropdown("skew", "false", ["false", "true"])
dbutils.widgets.text("horizon", "14")
dbutils.widgets.text("partitions", "0")  # 0 = one per group

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
n_groups = int(dbutils.widgets.get("n_groups"))
skew = dbutils.widgets.get("skew") == "true"
horizon = int(dbutils.widgets.get("horizon"))

# COMMAND ----------

import os
import sys
import time
from functools import partial

import cloudpickle

sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
import bench  # noqa: E402
import forecasting  # noqa: E402

partitions = bench.resolve_partitions(int(dbutils.widgets.get("partitions")), n_groups)

cloudpickle.register_pickle_by_value(forecasting)

source = f"{catalog}.{schema}.{forecasting.source_table_name(n_groups, skew)}"
spark.conf.set("spark.sql.adaptive.enabled", "false")  # keep exactly `partitions` tasks; no coalescing
cores = spark.sparkContext.defaultParallelism

# COMMAND ----------

# MAGIC %md ## Untimed: read and cache the input

# COMMAND ----------

sdf = spark.read.table(source).repartition(partitions, "group_name").cache()
print(f"{source}: {sdf.count():,} rows, {cores} cores, {partitions} partitions")

# Hash partitioning can't guarantee one group per partition; show how groups actually landed.
from pyspark.sql import functions as F  # noqa: E402

display(sdf.groupBy(F.spark_partition_id().alias("p")).agg(F.countDistinct("group_name").alias("groups"))
           .groupBy("groups").count().withColumnRenamed("count", "partitions").orderBy("groups"))

# COMMAND ----------

# MAGIC %md ## Timed: group → fit → forecast

# COMMAND ----------

t0 = time.perf_counter()
out = (sdf.groupBy("group_name")
          .applyInPandas(partial(forecasting.train_and_forecast, horizon=horizon), forecasting.FORECAST_SCHEMA)
          .cache())
out.count()
wall_s = time.perf_counter() - t0
print(f"train + forecast: {wall_s:.1f}s")

# COMMAND ----------

# MAGIC %md ## Untimed: record the result and write the forecasts

# COMMAND ----------

row = bench.result_row(engine="spark", n_groups=n_groups, skew=skew, cores=cores, partitions=partitions,
                       wall_s=wall_s, forecasts=out.select("group_name", "fit_s", "predict_s").toPandas())
spark.createDataFrame([row], bench.RESULT_SCHEMA).write.mode("append").saveAsTable(f"{catalog}.{schema}.benchmark_results")
out.write.mode("overwrite").saveAsTable(f"{catalog}.{schema}.forecasts_spark_{forecasting.source_table_name(n_groups, skew)}")
display(spark.createDataFrame([row], bench.RESULT_SCHEMA))
