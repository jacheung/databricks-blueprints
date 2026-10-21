# Databricks notebook source
# DBTITLE 1,Discover serving options
# MAGIC %md
# MAGIC # Discover serving options
# MAGIC
# MAGIC Lists which serving methods can serve a Hugging Face model in this workspace: PT (a `system.ai`
# MAGIC model that links to the HF repo, at its smallest capacity increment), pay-per-token (a shared
# MAGIC endpoint serving that model), and custom GPU (memory fit per single-GPU shape). Read-only: no
# MAGIC weights are downloaded, no endpoints are created or queried.
# MAGIC
# MAGIC Copy the `endpoint` blocks you want into `configs/<model>.yml`, then run `01_deploy`.
# MAGIC Same logic as `python discover.py` on a laptop.

# COMMAND ----------

import json
import sys

from databricks.sdk import WorkspaceClient

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, "/Workspace" + "/".join(_nb_path.split("/")[:-1]))
from discover import discover, to_markdown

dbutils.widgets.text("hf_name", "Alibaba-NLP/gte-large-en-v1.5")
dbutils.widgets.dropdown("task", "embedding", ["embedding", "chat"])

result = discover(dbutils.widgets.get("hf_name"), dbutils.widgets.get("task"), WorkspaceClient())
print(json.dumps(result["model"]))
displayHTML(f"<pre>{to_markdown(result['options'])}</pre>")

# COMMAND ----------

# DBTITLE 1,Endpoint blocks for configs/<model>.yml (viable options)
import yaml

print(yaml.safe_dump({"model": result["model"],
                      "endpoints": [o["endpoint"] for o in result["options"] if o["viable"]]}, sort_keys=False))
