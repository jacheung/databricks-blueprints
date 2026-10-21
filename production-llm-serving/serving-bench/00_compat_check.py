# Databricks notebook source
# DBTITLE 1,Check the model runs on one AI environment
# MAGIC %md
# MAGIC # Check the model runs on one AI environment
# MAGIC
# MAGIC Loads the model from `config.yml` on this Serverless GPU and runs one inference: the same
# MAGIC check `01_deploy` does before creating anything. Creates no endpoints and registers nothing.
# MAGIC
# MAGIC Use it when `01_deploy` fails its model check: run one copy per AI env candidate in parallel
# MAGIC (each job on a different base environment), then deploy on the newest one that passes.
# MAGIC Always exits with JSON `{ok, ai_env, transformers, torch, cuda, gpu, error}` so the job succeeds
# MAGIC and the caller reads `ok`.
# MAGIC
# MAGIC **Compute:** Serverless GPU (A10) with base environment AI v`ai_env`.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install sentence-transformers pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import json
import sys

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
PROJECT_DIR = "/Workspace" + "/".join(_nb_path.split("/")[:-1])
sys.path.insert(0, PROJECT_DIR)
from common import ModelCheckError, library_versions, load_and_check, load_config

dbutils.widgets.text("config_path", "")  # e.g. {PROJECT_DIR}/configs/<model>.yml
dbutils.widgets.text("ai_env", "")  # the env this job runs on; reported back, not switched
if not dbutils.widgets.get("config_path"):
    raise ValueError(f"Set the config_path widget, e.g. {PROJECT_DIR}/configs/<model>.yml")
MODEL = load_config(dbutils.widgets.get("config_path"))["model"]
AI_ENV = dbutils.widgets.get("ai_env")

try:
    _, info = load_and_check(MODEL, AI_ENV)
    result = {"ok": True, "error": None} | info
except ModelCheckError as error:
    result = {"ok": False, "ai_env": AI_ENV, "error": str(error).split("\n")[0]} | library_versions()
print(json.dumps(result, indent=2))
dbutils.notebook.exit(json.dumps(result))
