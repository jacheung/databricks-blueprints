# Databricks notebook source
# MAGIC %md
# MAGIC # 03 · Readout
# MAGIC
# MAGIC Latest run per (groups, sizes, engine) from `benchmark_results`, as a markdown table.

# COMMAND ----------

dbutils.widgets.text("catalog", "main")
dbutils.widgets.text("schema", "jon_cheung")
results_table = f"{dbutils.widgets.get('catalog')}.{dbutils.widgets.get('schema')}.benchmark_results"

# COMMAND ----------

import os
import sys

sys.path.insert(0, os.path.join(os.getcwd(), "lib"))
import bench  # noqa: E402

results = spark.table(results_table).toPandas()
display(results.sort_values("run_at"))
markdown = bench.readout_markdown(results)
print(markdown)
dbutils.notebook.exit(markdown)
