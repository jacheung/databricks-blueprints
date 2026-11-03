# Databricks notebook source
# DBTITLE 1,Check the workspace can run this blueprint
# MAGIC %md
# MAGIC # Check the workspace can run this blueprint
# MAGIC
# MAGIC Validates the config (`shared/configs/<target>.yml`) against the live workspace before anything is built:
# MAGIC auth, Unity Catalog location, Feature Engineering version, Lakebase online store API, and the embedding
# MAGIC endpoint. Creates the schema and volume if missing; creates nothing else.
# MAGIC
# MAGIC Prints a pass/warn/fail table and the resolved `auto` values. Exits with JSON `{ok, checks, resolved}`.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+ (or DBR 17.0 ML+).

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.16.0" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys
from importlib.metadata import version

from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_shared.config import load_config, resolve_auto, volume_path

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
UC = CFG["unity_catalog"]
w = WorkspaceClient()

checks = []  # (check, status, detail); status is PASS, WARN or FAIL


def record(check, status, detail=""):
    checks.append({"check": check, "status": status, "detail": str(detail)})

# COMMAND ----------

# DBTITLE 1,Auth and workspace
user = w.current_user.me().user_name
metastore = w.metastores.summary()
record("auth", "PASS", user)
record("workspace host", "PASS" if w.config.host.rstrip("/") == CFG["workspace"]["host"].rstrip("/") else "FAIL",
       f"running on {w.config.host}, config says {CFG['workspace']['host']}")
RESOLVED = resolve_auto(CFG, user)

# COMMAND ----------

# DBTITLE 1,Unity Catalog: catalog storage, schema, volume
catalog = w.catalogs.get(UC["catalog"])
# Streaming Feature Views need the catalog on your own cloud storage, not default storage.
# A catalog with no location of its own inherits the metastore root.
location = catalog.storage_location or catalog.storage_root or metastore.storage_root or ""
own_storage = location.startswith(("s3://", "abfss://", "gs://"))
record("catalog on own cloud storage", "PASS" if own_storage else "WARN", location or "no storage location reported")

spark.sql(f"CREATE SCHEMA IF NOT EXISTS `{UC['catalog']}`.`{UC['schema']}`")
spark.sql(f"CREATE VOLUME IF NOT EXISTS `{UC['catalog']}`.`{UC['schema']}`.`{UC['volume']}`")
record("schema and volume", "PASS", volume_path(CFG))

# COMMAND ----------

# DBTITLE 1,Feature Engineering and the online store
fe_version = version("databricks-feature-engineering")
fe_ok = tuple(int(p) for p in fe_version.split(".")[:2]) >= (0, 16)
record("databricks-feature-engineering >= 0.16", "PASS" if fe_ok else "FAIL", fe_version)

from databricks.feature_engineering import FeatureEngineeringClient

store_name = CFG["feature_store"]["online_store"]
try:
    # Returns None (does not raise) when the store doesn't exist yet; feature_views creates it.
    store = FeatureEngineeringClient().get_online_store(name=store_name)
    record("online store API", "PASS", f"{store_name}: {'exists' if store else 'not created yet'}")
except Exception as error:  # preview not enabled or Lakebase unavailable in this region
    record("online store API", "FAIL", str(error).split("\n")[0])

# COMMAND ----------

# DBTITLE 1,Embedding endpoint
embedding_endpoint = CFG["ai_search"]["embedding_endpoint"]
try:
    state = w.serving_endpoints.get(embedding_endpoint).state.ready
    record("embedding endpoint", "PASS" if state and state.value == "READY" else "WARN", f"{embedding_endpoint}: {state}")
except NotFound:
    record("embedding endpoint", "FAIL", f"{embedding_endpoint} not found")

# COMMAND ----------

# DBTITLE 1,Results
import pandas as pd

display(pd.DataFrame(checks))
print("Resolved auto values:")
print(f"  mlflow.experiment       = {RESOLVED['mlflow']['experiment']}")
result = {"ok": not any(c["status"] == "FAIL" for c in checks), "checks": checks,
          "resolved": {"mlflow.experiment": RESOLVED["mlflow"]["experiment"]}}
dbutils.notebook.exit(json.dumps(result))
