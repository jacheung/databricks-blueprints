# Databricks notebook source
# MAGIC %md
# MAGIC # Appendix A1 · Test: does each index only see changes to its own columns?
# MAGIC
# MAGIC Optional verification, not part of the pattern. Run after `01`. It also creates a **comparison index**
# MAGIC directly on `docs_wide` (`columns_to_sync=["title", "body"]`) to show what happens without the split.
# MAGIC
# MAGIC Three scenarios each edit one column group in `docs_wide`, then rerun the narrow-table refresh:
# MAGIC
# MAGIC | Scenario | Rows | Expected changes: Index A (title) | Index B (category) | Naive index (on `docs_wide`) |
# MAGIC |---|---|---|---|---|
# MAGIC | Change `category` only | 100 | 0 | 100 | 100 (it doesn't sync `category`, but still sees the rows) |
# MAGIC | Change `title` only | 100 | 100 | 0 | 100 |
# MAGIC | Change `body` (+ re-embed) | 50 | 50 | 50 | 50 |
# MAGIC
# MAGIC "Changes" = rows in that index's source change feed (CDF), which is what its next sync has to process.
# MAGIC After each scenario all three indexes are synced, and the notebook reports what each sync processed.

# COMMAND ----------

# MAGIC %pip install -q databricks-ai-search
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog_name", "main", "Catalog")
dbutils.widgets.text("schema_name", "split_table_vs", "Schema")
dbutils.widgets.text("endpoint_name", "split-table-vs-demo", "AI Search endpoint")

catalog = dbutils.widgets.get("catalog_name")
schema = dbutils.widgets.get("schema_name")
endpoint_name = dbutils.widgets.get("endpoint_name")
fq_schema = f"{catalog}.{schema}"
source_table = f"{fq_schema}.docs_wide"

import os
import sys

sys.path.append(os.path.abspath(".."))  # narrow_tables.py lives one folder up
from narrow_tables import EMBEDDING_MODEL, refresh_narrow_tables, latest_version, cdf_rows_since, ensure_index

# COMMAND ----------

import datetime
import json
from databricks.ai_search.client import AISearchClient

client = AISearchClient(disable_notice=True)
ensure_index(client, endpoint_name, f"{fq_schema}.docs_wide_naive_index", source_table, ["title", "body"])

# label -> (index name, its source table)
targets = {
    "index_a_title": (f"{fq_schema}.docs_idx_title_index", f"{fq_schema}.docs_idx_title"),
    "index_b_category": (f"{fq_schema}.docs_idx_category_index", f"{fq_schema}.docs_idx_category"),
    "naive_on_wide": (f"{fq_schema}.docs_wide_naive_index", source_table),
}
indexes = {k: client.get_index(endpoint_name=endpoint_name, index_name=v[0]) for k, v in targets.items()}


def sync_all():
    """Trigger a sync on every index, wait for each, and return its post-sync status."""
    for idx in indexes.values():
        idx.sync()
    out = {}
    for label, idx in indexes.items():
        idx.wait_until_ready(wait_for_updates=True, timeout=datetime.timedelta(minutes=30))
        out[label] = idx.describe().get("status", {})
    return out

# COMMAND ----------

scenarios = [
    ("category only (C)", 100, f"""
        UPDATE {source_table} SET category = concat(category, '_v2')
        WHERE CAST(id AS INT) < 100"""),
    ("title only (A)", 100, f"""
        UPDATE {source_table} SET title = concat(title, ' (revised)')
        WHERE CAST(id AS INT) BETWEEN 100 AND 199"""),
    # The upstream writer owns vector freshness: when body changes, re-embed it in the same write.
    # ai_query isn't allowed inside UPDATE/MERGE (non-deterministic), so embed the edited rows into a
    # staging table first, then MERGE text + vector into the source together.
    ("body + re-embed (B)", 50, [f"""
        CREATE OR REPLACE TABLE {fq_schema}._body_edits AS
        SELECT id, body, CAST(ai_query('{EMBEDDING_MODEL}', body) AS ARRAY<FLOAT>) AS body_vector
        FROM (SELECT id, concat(body, ' Updated.') AS body FROM {source_table}
              WHERE CAST(id AS INT) BETWEEN 200 AND 249)""", f"""
        MERGE INTO {source_table} t USING {fq_schema}._body_edits s ON t.id = s.id
        WHEN MATCHED THEN UPDATE SET t.body = s.body, t.body_vector = s.body_vector"""]),
]
expected = {
    "category only (C)": {"index_a_title": 0, "index_b_category": 100, "naive_on_wide": 100},
    "title only (A)": {"index_a_title": 100, "index_b_category": 0, "naive_on_wide": 100},
    "body + re-embed (B)": {"index_a_title": 50, "index_b_category": 50, "naive_on_wide": 50},
}

# Start from idle indexes: a sync can't be triggered while one is already running.
for idx in indexes.values():
    idx.wait_until_ready(wait_for_updates=True, timeout=datetime.timedelta(minutes=30))

results = []
sync_status = {}
for name, n_rows, update_sql in scenarios:
    before = {label: latest_version(spark, table) for label, (_, table) in targets.items()}
    for stmt in ([update_sql] if isinstance(update_sql, str) else update_sql):
        spark.sql(stmt)
    refresh_narrow_tables(spark, fq_schema, source_table)
    for label, (_, table) in targets.items():
        results.append({
            "scenario": name,
            "index": label,
            "expected_cdf_rows": expected[name][label],
            "actual_cdf_rows": cdf_rows_since(spark, table, before[label]),
        })
    sync_status[name] = sync_all()

# COMMAND ----------

import pandas as pd

res = pd.DataFrame(results)
res["pass"] = res.expected_cdf_rows == res.actual_cdf_rows
display(res)

# COMMAND ----------

# MAGIC %md
# MAGIC ## What each sync reported

# COMMAND ----------

print(json.dumps(sync_status, indent=2, default=str))

# COMMAND ----------

spark.sql(f"DROP TABLE IF EXISTS {fq_schema}._body_edits")
assert res["pass"].all(), "Some index saw changes it shouldn't have (or missed ones it should)"
dbutils.notebook.exit(json.dumps({"results": results, "sync_status": sync_status}, default=str))
