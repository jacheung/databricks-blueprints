# Databricks notebook source
# MAGIC %md
# MAGIC # 00 · Generate synthetic hourly time series
# MAGIC
# MAGIC One table with `n_groups` hourly series (`ds`, `group_name`, `features_0..1`, `y`).
# MAGIC With `skew=true`, rows per group are log-normal around `rows_per_group` instead of equal.

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "jon_cheung")
dbutils.widgets.text("n_groups", "10")
dbutils.widgets.text("rows_per_group", "50000")
dbutils.widgets.dropdown("skew", "false", ["false", "true"])

catalog = dbutils.widgets.get("catalog")
schema = dbutils.widgets.get("schema")
n_groups = int(dbutils.widgets.get("n_groups"))
mean_rows = int(dbutils.widgets.get("rows_per_group"))
skew = dbutils.widgets.get("skew") == "true"

# COMMAND ----------

import os
import sys

import cloudpickle

sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
import forecasting  # noqa: E402

cloudpickle.register_pickle_by_value(forecasting)  # ship lib code to workers by value
table = f"{catalog}.{schema}.{forecasting.source_table_name(n_groups, skew)}"

# COMMAND ----------

spark.sql(f"CREATE SCHEMA IF NOT EXISTS {catalog}.{schema}")

groups = spark.createDataFrame(
    [(f"group_{i + 1:05d}", n) for i, n in enumerate(forecasting.rows_per_group(n_groups, mean_rows, skew=skew))],
    "group_name string, n_rows int",
)


def generate(pdf):
    import pandas as pd

    return pd.concat([forecasting.make_group_series(g, int(n)) for g, n in pdf.itertuples(index=False)])


schema_ddl = "ds timestamp, group_name string, features_0 double, features_1 double, y double"
(groups.groupBy("group_name").applyInPandas(generate, schema_ddl)
       .write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(table))

display(spark.sql(f"SELECT count(*) AS rows, count(DISTINCT group_name) AS groups FROM {table}"))
