# Databricks notebook source
# MAGIC %md
# MAGIC # Appendix A2 · Can `model_endpoint_name_for_query` make `query_text` work on a self-managed index?
# MAGIC
# MAGIC Optional, not part of the pattern; run after `01`. The answer is no (see the README), and this notebook is
# MAGIC the evidence. It creates two temporary indexes (several minutes and some endpoint cost), then deletes them.

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

import datetime
import json
from databricks.ai_search.client import AISearchClient
import os
import sys

sys.path.append(os.path.abspath(".."))  # narrow_tables.py lives one folder up
from narrow_tables import EMBEDDING_MODEL, EMBEDDING_DIM, PRIMARY_KEY

client = AISearchClient(disable_notice=True)
index_a = client.get_index(endpoint_name=endpoint_name, index_name=f"{fq_schema}.docs_idx_title_index")
findings = {}

# COMMAND ----------

# MAGIC %md
# MAGIC ## The supported way: embed the query, pass `query_vector`

# COMMAND ----------

def embed(text):
    return spark.sql(f"SELECT ai_query('{EMBEDDING_MODEL}', :t) AS v", args={"t": text}).first()["v"]

query = "how do I turn on two-factor login"
hits = index_a.similarity_search(query_vector=embed(query), columns=["id", "title", "body"], num_results=5)
rows = hits["result"]["data_array"]
for r in rows:
    print(r)
findings["query_vector_top_titles"] = [r[1] for r in rows]

# COMMAND ----------

# MAGIC %md
# MAGIC ## `query_text` on a self-managed index (no model attached)

# COMMAND ----------

def try_query_text(idx):
    try:
        res = idx.similarity_search(query_text=query, columns=["id", "title"], num_results=5)
        return {"ok": True, "top_titles": [r[1] for r in res["result"]["data_array"]]}
    except Exception as e:
        return {"ok": False, "error": str(e)[:400]}

findings["query_text_plain_index"] = try_query_text(index_a)
print(findings["query_text_plain_index"])

# COMMAND ----------

# MAGIC %md
# MAGIC ## Probe 1: pass `model_endpoint_name_for_query` through the Python SDK

# COMMAND ----------

probe_sdk = f"{fq_schema}.docs_idx_title_probe_sdk_index"
client.create_delta_sync_index(
    endpoint_name=endpoint_name,
    index_name=probe_sdk,
    source_table_name=f"{fq_schema}.docs_idx_title",
    primary_key=PRIMARY_KEY,
    pipeline_type="TRIGGERED",
    embedding_dimension=EMBEDDING_DIM,
    embedding_vector_column="body_vector",
    model_endpoint_name_for_query=EMBEDDING_MODEL,
)
probe_idx = client.get_index(endpoint_name=endpoint_name, index_name=probe_sdk)
probe_idx.wait_until_ready(timeout=datetime.timedelta(minutes=45))

findings["probe_sdk_spec"] = probe_idx.describe().get("delta_sync_index_spec")
findings["probe_sdk_query_text"] = try_query_text(probe_idx)
print(json.dumps({k: findings[k] for k in ["probe_sdk_spec", "probe_sdk_query_text"]}, indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Probe 2: send the field straight to the REST API
# MAGIC The SDK builds the request body itself, so this checks what the API does with the field
# MAGIC when it's set on the embedding vector column.

# COMMAND ----------

from databricks.sdk import WorkspaceClient

w = WorkspaceClient()
probe_rest = f"{fq_schema}.docs_idx_title_probe_rest_index"
body = {
    "name": probe_rest,
    "endpoint_name": endpoint_name,
    "primary_key": PRIMARY_KEY,
    "index_type": "DELTA_SYNC",
    "delta_sync_index_spec": {
        "source_table": f"{fq_schema}.docs_idx_title",
        "pipeline_type": "TRIGGERED",
        "embedding_vector_columns": [{
            "name": "body_vector",
            "embedding_dimension": EMBEDDING_DIM,
            "model_endpoint_name_for_query": EMBEDDING_MODEL,
        }],
    },
}
try:
    resp = w.api_client.do("POST", "/api/2.0/vector-search/indexes", body=body)
    findings["probe_rest_create"] = {"ok": True, "spec": resp.get("delta_sync_index_spec")}
    rest_idx = client.get_index(endpoint_name=endpoint_name, index_name=probe_rest)
    rest_idx.wait_until_ready(timeout=datetime.timedelta(minutes=45))
    findings["probe_rest_query_text"] = try_query_text(rest_idx)
except Exception as e:
    findings["probe_rest_create"] = {"ok": False, "error": str(e)[:400]}
print(json.dumps({k: v for k, v in findings.items() if k.startswith("probe_rest")}, indent=2))

# COMMAND ----------

# MAGIC %md
# MAGIC ## Clean up the probe indexes

# COMMAND ----------

for name in [probe_sdk, probe_rest]:
    try:
        client.delete_index(endpoint_name=endpoint_name, index_name=name)
        print("deleted", name)
    except Exception as e:
        print("skip", name, str(e)[:120])

dbutils.notebook.exit(json.dumps(findings, default=str))
