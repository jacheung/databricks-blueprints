# Databricks notebook source
# DBTITLE 1,Grant the app's service principal read access in Lakebase
# MAGIC %md
# MAGIC # Grant the app's service principal read access in Lakebase
# MAGIC
# MAGIC The app's Lakebase resource gives its service principal a Postgres role that can connect but can't read our
# MAGIC schema. This grants it `SELECT`, now and for tables created later in the schema, in both databases it reads:
# MAGIC - `lakebase.database`: the synced tables (QAC prefixes, demo scenarios);
# MAGIC - the Feature Store online tables' database, named after the catalog: the session features, which the app reads
# MAGIC   back after a scenario Play to show how fresh they are.
# MAGIC
# MAGIC Run it after the first `bundle run rts_app`, as the schemas' owner.
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
from rts_shared.lakebase import connect, features_database

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")
dbutils.widgets.text("app_name", "rts-search")
CFG = load_config(dbutils.widgets.get("config_path"))
w = WorkspaceClient()
role = w.apps.get(name=dbutils.widgets.get("app_name")).service_principal_client_id
schema = CFG["unity_catalog"]["schema"]
synced = [CFG["tables"][k] for k in ("qac_prefix_online", "demo_scenarios_online")]
# The session Feature Views' online tables carry a generated suffix (rts_session_online_<suffix>).
SESSION_TABLES = "SELECT relname FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace " \
                 "WHERE n.nspname = :s AND c.relkind IN ('r', 'p') AND relname LIKE 'rts_session_online%'"

# COMMAND ----------

granted = {}
for database in (CFG["lakebase"]["database"], features_database(CFG)):
    conn = connect(w, CFG, database)
    try:
        conn.run(f'GRANT CONNECT ON DATABASE "{database}" TO "{role}"')
        conn.run(f'GRANT USAGE ON SCHEMA "{schema}" TO "{role}"')
        conn.run(f'GRANT SELECT ON ALL TABLES IN SCHEMA "{schema}" TO "{role}"')
        conn.run(f'ALTER DEFAULT PRIVILEGES IN SCHEMA "{schema}" GRANT SELECT ON TABLES TO "{role}"')
        tables = synced if database == CFG["lakebase"]["database"] else [r[0] for r in conn.run(SESSION_TABLES, s=schema)]
        assert tables, f"no tables to check in {database}.{schema}"
        granted |= {f"{database}.{t}": conn.run("SELECT has_table_privilege(:r, :t, 'SELECT')", r=role,
                                                  t=f'"{schema}"."{t}"')[0][0] for t in tables}
    finally:
        conn.close()
assert all(granted.values()), f"SELECT not effective for {role}: {granted}"
print(json.dumps({"role": role, "granted": granted}, indent=2))
dbutils.notebook.exit(json.dumps({"role": role, "granted": granted}))
