# Databricks notebook source
# MAGIC %md
# MAGIC # Path 1 · No saved models: train and forecast in one pass
# MAGIC
# MAGIC Use this when training and forecasting run on the same schedule (say, a nightly job):
# MAGIC there's nothing to store or serve. Fits one Prophet model per group with Spark
# MAGIC `applyInPandas` and writes only the forecasts. Runs on serverless or classic compute.
# MAGIC
# MAGIC Run `00_generate_timeseries_data` first.

# COMMAND ----------

# MAGIC %pip install -q -r lib/requirements.txt

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

# COMMAND ----------

import os
import sys
from functools import partial

import cloudpickle

sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
import forecasting  # noqa: E402

cloudpickle.register_pickle_by_value(forecasting)
# Partitioning: we use partitions = n_groups, i.e. one group per task on average (hashing puts
# some groups together). The right count depends on how long one group's fit takes, which
# depends on both the model and the rows per group:
# - Fits of seconds to minutes (Prophet here: about 1-350 s per group): use partitions = n_groups.
#   Per-task overhead (milliseconds up to ~0.1 s) is tiny next to the fit, so finer partitions
#   cost almost nothing and stop one large group from holding up the others packed with it.
# - Sub-second fits (small models, simple regressions): overhead starts to dominate. Use about
#   2-4x the core count so each task carries enough work.
# - Very many groups: one partition per group means a lot of shuffle and scheduling metadata.
#   100k partitions (100k groups, ~2 s fits) ran without trouble on serverless in our test; well
#   beyond that, or with short fits, cap at about 10-20x the core count to keep the tail short
#   without the bookkeeping.
# Packing only hurts when group sizes vary: with uniform groups, any of these behave about the same.
n_groups = int(dbutils.widgets.get("n_groups"))
suffix = forecasting.source_table_name(n_groups, dbutils.widgets.get("skew") == "true")
source_table = f"{catalog}.{schema}.{suffix}"
forecasts_table = f"{catalog}.{schema}.forecasts_{suffix}"
backtest_table = f"{catalog}.{schema}.backtest_{suffix}"
fn = partial(forecasting.train_and_forecast, horizon=horizon)

# COMMAND ----------

forecasts = (spark.read.table(source_table)
                  .repartition(n_groups, "group_name")
                  .groupBy("group_name")
                  .applyInPandas(fn, forecasting.FORECAST_SCHEMA))

forecasts.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(forecasts_table)
display(spark.sql(f"SELECT count(DISTINCT group_name) AS groups, count(*) AS rows FROM {forecasts_table}"))

# COMMAND ----------

# MAGIC %md ## Backtest at the chosen horizon
# MAGIC
# MAGIC For each group, hold out the last `horizon` rows, fit on the rest, and compare the forecast
# MAGIC with what happened: MAE, RMSE, and `coverage` (the share of actuals inside the 80% interval;
# MAGIC near 0.8 means the intervals are calibrated). This refits every group, so it takes about as
# MAGIC long as the forecast above.
# MAGIC
# MAGIC This is **one window**: it tells you the error at this horizon from one cutoff, not how
# MAGIC stable that error is. Before trusting the model, repeat it over several cutoffs (a
# MAGIC rolling-origin backtest, e.g. `prophet.diagnostics.cross_validation`) to see how much
# MAGIC performance moves over time, and over several horizons to see where it starts to drop.

# COMMAND ----------

bt = partial(forecasting.backtest, horizon=horizon)
backtests = (spark.read.table(source_table)
                  .repartition(n_groups, "group_name")
                  .groupBy("group_name")
                  .applyInPandas(bt, forecasting.BACKTEST_SCHEMA))

backtests.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(backtest_table)
display(spark.sql(f"""SELECT count(*) AS groups, avg(mae) AS avg_mae, percentile(mae, 0.5) AS p50_mae,
                             percentile(mae, 0.9) AS p90_mae, avg(rmse) AS avg_rmse, avg(coverage) AS avg_coverage
                      FROM {backtest_table}"""))
