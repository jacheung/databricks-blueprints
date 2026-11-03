# Databricks notebook source
# DBTITLE 1,Grant the app's service principal read access in Lakebase
# MAGIC %md
# MAGIC # Grant the app's service principal read access in Lakebase
# MAGIC
# MAGIC The app's Lakebase resource gives its service principal a Postgres role that can connect but can't read our
# MAGIC schema. This grants it `SELECT` on the synced tables the app reads (QAC prefixes, demo scenarios), now and for
# MAGIC tables created later in the schema. Run it after the first `bundle run rts_app`, as the schema's owner.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# MAGIC %pip install "databricks-sdk>=0.81" "pg8000>=1.31" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

import json
import sys

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/app/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src"]
from rts_shared.config import load_config
from rts_shared.lakebase import connect

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")
dbutils.widgets.text("app_name", "rts-search")
CFG = load_config(dbutils.widgets.get("config_path"))
w = WorkspaceClient()
role = w.apps.get(name=dbutils.widgets.get("app_name")).service_principal_client_id
schema = CFG["unity_catalog"]["schema"]
tables = [CFG["tables"][k] for k in ("qac_prefix_online", "demo_scenarios_online")]

# COMMAND ----------

conn = connect(w, CFG)
try:
    conn.run(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
    conn.run(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"')
    conn.run(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT SELECT ON TABLES TO "{role}"')
    granted = {t: conn.run("SELECT has_table_privilege(:r, :t, 'SELECT')", r=role, t=f'"{schema}"."{t}"')[0][0]
               for t in tables}
finally:
    conn.close()
assert all(granted.values()), f"SELECT not effective for {role}: {granted}"
print(json.dumps({"role": role, "granted": granted}))
dbutils.notebook.exit(json.dumps({"role": role, "granted": granted}))
