# Databricks notebook source
# MAGIC %md
# MAGIC # 02 · Query the indexes
# MAGIC
# MAGIC The indexes store vectors you computed and have no embedding model attached, so `query_text` doesn't work.
# MAGIC Embed the query yourself, **with the same model used for `body_vector`**, and pass it as `query_vector`.

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

from databricks.ai_search.client import AISearchClient
from narrow_tables import EMBEDDING_MODEL

client = AISearchClient(disable_notice=True)
index_a = client.get_index(endpoint_name=endpoint_name, index_name=f"{fq_schema}.docs_idx_title_index")
index_b = client.get_index(endpoint_name=endpoint_name, index_name=f"{fq_schema}.docs_idx_category_index")


def embed(text):
    """Embed a query with the same model that produced body_vector."""
    return spark.sql(f"SELECT ai_query('{EMBEDDING_MODEL}', :t) AS v", args={"t": text}).first()["v"]

# COMMAND ----------

# MAGIC %md
# MAGIC ## Index A: search, return titles

# COMMAND ----------

q = embed("how do I turn on two-factor login")
hits = index_a.similarity_search(query_vector=q, columns=["id", "title", "body"], num_results=5)
for row in hits["result"]["data_array"]:
    print(row)

# COMMAND ----------

# MAGIC %md
# MAGIC ## Index B: search within a category

# COMMAND ----------

q = embed("my invoice looks wrong")
hits = index_b.similarity_search(query_vector=q, columns=["id", "category", "body"],
                                 filters={"category": "billing"}, num_results=5)
for row in hits["result"]["data_array"]:
    print(row)
