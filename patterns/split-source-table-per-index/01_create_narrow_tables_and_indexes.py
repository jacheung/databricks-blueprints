# Databricks notebook source
# MAGIC %md
# MAGIC # 01 · Narrow tables and their indexes
# MAGIC
# MAGIC ```
# MAGIC docs_wide (id, title, body, category, body_vector)
# MAGIC   │  conditional MERGE (narrow_tables.refresh_narrow_tables)
# MAGIC   ├──► docs_idx_title    (id, title, body, body_vector)    ──► Index A
# MAGIC   └──► docs_idx_category (id, body, category, body_vector) ──► Index B
# MAGIC ```
# MAGIC
# MAGIC Both indexes use **self-managed embeddings**: they read `body_vector` and never call an embedding model.

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

from narrow_tables import NARROW_TABLES, create_narrow_tables, refresh_narrow_tables, ensure_index

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create and fill the narrow tables

# COMMAND ----------

create_narrow_tables(spark, fq_schema, source_table)
refresh_narrow_tables(spark, fq_schema, source_table)

for name in NARROW_TABLES:
    print(f"{fq_schema}.{name}: {spark.table(f'{fq_schema}.{name}').count()} rows")

# COMMAND ----------

# MAGIC %md
# MAGIC ## Create the indexes
# MAGIC Same call as managed embeddings, but with `embedding_vector_column` + `embedding_dimension`
# MAGIC instead of `embedding_source_column` + `embedding_model_endpoint_name`.

# COMMAND ----------

from databricks.ai_search.client import AISearchClient

client = AISearchClient(disable_notice=True)
for name in NARROW_TABLES:
    ensure_index(client, endpoint_name, f"{fq_schema}.{name}_index", f"{fq_schema}.{name}")
