# Databricks notebook source
# DBTITLE 1,Demo personas: who the app signs in as
# MAGIC %md
# MAGIC # Demo personas: who the app signs in as
# MAGIC
# MAGIC The app's Sign in offers `app.demo_personas` (a name and a category each) plus Continue as guest. Signing in is
# MAGIC decoupled from the replayed clickstream: a persona brings long-term user history (the 30-day user features), a
# MAGIC replay brings in-session intent, and the two combine freely.
# MAGIC
# MAGIC Each persona gets the simulated user most concentrated in its category in the user features' **latest values**,
# MAGIC with at least `app.persona_min_events` events there (`rts_shared.scenarios.pick_personas`). Step 0 (2026-11-05)
# MAGIC showed the L2 lift from user history comes through category share, not median price, so personas differ by
# MAGIC category. The table is synced to Lakebase (snapshot) so the app reads it with the scenarios.
# MAGIC
# MAGIC The user features slide with the calendar while the simulated history doesn't (see the stale batch features
# MAGIC gotcha in `data/PLAN.md`), so `features_as_of` and the per-persona event counts record what the picks stood on.
# MAGIC Rerun after `clickstream_job` refreshes the history. Reruns are safe: the table is overwritten and re-synced.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-sdk>=0.81" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys

from pyspark.sql import functions as F

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_shared.config import load_config, table
from rts_shared.lakebase import sync_snapshot
from rts_shared.scenarios import PERSONA_COLUMNS, pick_personas

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
WANTED, MIN_EVENTS = CFG["app"]["demo_personas"], CFG["app"]["persona_min_events"]
PERSONAS, ONLINE = table(CFG, "demo_personas"), table(CFG, "demo_personas_online")
UC = CFG["unity_catalog"]

# COMMAND ----------

# DBTITLE 1,User features: latest values
# The user Feature Views' offline tables carry a generated suffix; find each one by the feature column it holds.
views = [r["tableName"] for r in spark.sql(f"SHOW TABLES IN {UC['catalog']}.{UC['schema']} LIKE 'rts_user_offline_*_latest_view'").collect()]
by_feature = {}
for v in views:
    name = f"{UC['catalog']}.{UC['schema']}.{v}"
    for c in spark.table(name).columns:
        if c.startswith("user_"):
            by_feature[c] = name
print(by_feature)
cats = spark.table(by_feature["user_cat_cnt_30d"])
category_rows = [(r["user_id"], r["category"], int(r["user_cat_cnt_30d"]))
                 for r in cats.where("user_cat_cnt_30d > 0").select("user_id", "category", "user_cat_cnt_30d").collect()]
users = {r["user_id"]: {"user_n_clicks_30d": int(r["user_n_clicks_30d"] or 0),
                        "user_median_price_30d": float(r["user_median_price_30d"] or 0)}
         for r in (spark.table(by_feature["user_n_clicks_30d"])
                   .join(spark.table(by_feature["user_median_price_30d"]), "user_id").collect())}
# The latest view drops the window-end timestamp; its base offline table keeps one row per daily window.
as_of = spark.table(by_feature["user_cat_cnt_30d"].removesuffix("_latest_view")).agg(F.max("event_time")).first()[0]
print(f"{len(users)} users with history; features as of {as_of}")

# COMMAND ----------

# DBTITLE 1,Pick personas
picks = [{**p, "features_as_of": as_of} for p in pick_personas(WANTED, category_rows, users, MIN_EVENTS)]
for p in picks:
    print(p)

schema = ("persona_id string NOT NULL, name string, user_id string, category string, category_share double, "
          "category_events_30d int, n_clicks_30d int, median_price_30d double, features_as_of timestamp")
df = spark.createDataFrame([tuple(p[c] for c in PERSONA_COLUMNS) for p in picks], schema)
df.write.mode("overwrite").option("overwriteSchema", "true").saveAsTable(PERSONAS)

# COMMAND ----------

# DBTITLE 1,Checks: one distinct user per persona, each concentrated in its category
assert len(picks) == len(WANTED), f"{len(picks)}/{len(WANTED)} personas"
assert len({p["user_id"] for p in picks}) == len(picks), "a user was picked twice"
for p in picks:
    assert p["category_events_30d"] >= MIN_EVENTS and p["category_share"] >= 0.5, p

# COMMAND ----------

# DBTITLE 1,Lakebase synced table (snapshot)
w = WorkspaceClient()
status = sync_snapshot(w, CFG, PERSONAS, ONLINE, ["persona_id"])
online = spark.table(ONLINE).count()
assert online == len(picks), f"Lakebase has {online} personas, Delta {len(picks)}"
print(ONLINE, status.detailed_state)

summary = {"personas": [{k: p[k] for k in ("name", "user_id", "category", "category_share", "category_events_30d")}
                        for p in picks], "features_as_of": str(as_of), "online_table": ONLINE}
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
