# Databricks notebook source
# MAGIC %md
# MAGIC # 99 · Clean up
# MAGIC Deletes the indexes, the schema (with all its tables), and optionally the endpoint.

# COMMAND ----------

# MAGIC %pip install -q databricks-ai-search
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

dbutils.widgets.text("catalog_name", "main", "Catalog")
dbutils.widgets.text("schema_name", "split_table_vs", "Schema")
dbutils.widgets.text("endpoint_name", "split-table-vs-demo", "AI Search endpoint")
dbutils.widgets.dropdown("delete_endpoint", "false", ["true", "false"], "Also delete endpoint")

catalog = dbutils.widgets.get("catalog_name")
schema = dbutils.widgets.get("schema_name")
endpoint_name = dbutils.widgets.get("endpoint_name")
fq_schema = f"{catalog}.{schema}"

from databricks.ai_search.client import AISearchClient

client = AISearchClient(disable_notice=True)
for idx in client.list_indexes(endpoint_name).get("vector_indexes", []):
    if idx["name"].startswith(f"{fq_schema}."):
        client.delete_index(endpoint_name=endpoint_name, index_name=idx["name"])
        print("deleted index", idx["name"])

spark.sql(f"DROP SCHEMA IF EXISTS {fq_schema} CASCADE")
print("dropped schema", fq_schema)

if dbutils.widgets.get("delete_endpoint") == "true":
    client.delete_endpoint(endpoint_name)
    print("deleted endpoint", endpoint_name)
