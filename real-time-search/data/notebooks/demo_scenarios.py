# Databricks notebook source
# DBTITLE 1,Demo scenarios: replayable session starts
# MAGIC %md
# MAGIC # Demo scenarios: replayable session starts
# MAGIC
# MAGIC Picks `app.demo_scenarios` simulated **test-split** sessions (never used for training) with a strong planted
# MAGIC intent, and stores each one's events before its last search, plus that search's query, as a scenario
# MAGIC (`rts_shared.scenarios`). The app replays a scenario to Kafka under a fresh session id; the features and
# MAGIC rankings the demo shows are computed live from those events.
# MAGIC
# MAGIC Selection: the session's intent has a brand; before its last search it has 2+ searches, 3+ clicks and at least
# MAGIC one click on the intent brand. One scenario per intent category, half from logged-in users (user features
# MAGIC apply) and half from guests. The table is synced to Lakebase (snapshot) so the app reads it with the QAC table.
# MAGIC
# MAGIC Reruns are safe: the table is overwritten and re-synced.
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
from datetime import datetime, timezone

from pyspark.sql import functions as F
from pyspark.sql.window import Window

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_shared.config import load_config, table
from rts_shared.events import event_payload
from rts_shared.lakebase import sync_snapshot
from rts_shared.scenarios import SCENARIO_COLUMNS, SCENARIO_EVENT_FIELDS, replay

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
N = CFG["app"]["demo_scenarios"]
SCENARIOS, ONLINE = table(CFG, "demo_scenarios"), table(CFG, "demo_scenarios_online")

# COMMAND ----------

# DBTITLE 1,Candidate sessions
sessions = (spark.table(table(CFG, "sim_sessions"))
            .where("split = 'test' AND intent_brand IS NOT NULL")
            .select("session_id", "user_id", "intent_category", "intent_brand", "intent_price_target"))
# user_id comes from sim_sessions (the replay sets it on every event).
events = (spark.table(table(CFG, "session_events")).drop("user_id")
          .join(sessions.select("session_id"), "session_id"))
last_search = (events.where("event_type = 'search'").groupBy("session_id")
               .agg(F.max_by("query", "event_time").alias("next_query"), F.max("event_time").alias("cut")))
before = (events.join(last_search, "session_id").where(F.col("event_time") < F.col("cut"))
          .join(sessions, "session_id"))
candidates = (before.groupBy("session_id", "user_id", "intent_category", "intent_brand", "intent_price_target",
                             "next_query")
              .agg(F.count_if(F.col("event_type") == "search").alias("searches"),
                   F.count_if(F.col("event_type") == "click").alias("clicks"),
                   F.count_if((F.col("event_type") == "click")
                              & (F.lower("brand") == F.lower("intent_brand"))).alias("brand_clicks"),
                   F.count_if((F.col("event_type") == "click")
                              & (F.col("category") == F.col("intent_category"))).alias("category_clicks"))
              .where("searches >= 2 AND clicks >= 3 AND brand_clicks >= 1"))
# The strongest session per (intent category, logged in or not).
best = (candidates.withColumn("logged_in", F.col("user_id").isNotNull())
        .withColumn("r", F.row_number().over(Window.partitionBy("intent_category", "logged_in")
                                             .orderBy(F.desc("brand_clicks"), F.desc("category_clicks"),
                                                      "session_id")))
        .where("r = 1").collect())

# Alternate logged-in and guest picks, strongest first, one per intent category.
pools = {flag: sorted((r for r in best if r["logged_in"] == flag),
                      key=lambda r: (-r["brand_clicks"], -r["category_clicks"], r["session_id"]))
         for flag in (True, False)}
picked, used = [], set()
while len(picked) < N and any(pools.values()):
    for flag in (True, False):
        while pools[flag] and pools[flag][0]["intent_category"] in used:
            pools[flag].pop(0)
        if pools[flag] and len(picked) < N:
            r = pools[flag].pop(0)
            picked.append(r)
            used.add(r["intent_category"])
print(f"{candidates.count()} candidate sessions; picked {len(picked)}")

# COMMAND ----------

# DBTITLE 1,Scenarios table
ids = [r["session_id"] for r in picked]
by_session = {}
for e in (before.where(F.col("session_id").isin(ids)).orderBy("session_id", "event_time", "event_id").collect()):
    by_session.setdefault(e["session_id"], []).append(e)

rows = []
for i, r in enumerate(picked):
    evs = by_session[r["session_id"]]
    t0 = evs[0]["event_time"]
    scenario_events = [{"offset_ms": int((e["event_time"] - t0).total_seconds() * 1000),
                        **{f: e[f] for f in SCENARIO_EVENT_FIELDS if f != "offset_ms"}} for e in evs]
    who = "logged-in user" if r["user_id"] else "guest"
    rows.append({"scenario_id": f"sc{i + 1}", "user_id": r["user_id"],
                 "label": f"{r['intent_brand']} {r['intent_category']} shopper, ~${r['intent_price_target']:.0f} ({who})",
                 "intent_category": r["intent_category"], "intent_brand": r["intent_brand"],
                 "intent_price_target": r["intent_price_target"], "next_query": r["next_query"],
                 "events": json.dumps(scenario_events)})

schema = ("scenario_id string NOT NULL, label string, user_id string, intent_category string, intent_brand string, "
          "intent_price_target double, next_query string, events string")
df = spark.createDataFrame([tuple(r[c] for c in SCENARIO_COLUMNS) for r in rows], schema)
df.write.mode("overwrite").saveAsTable(SCENARIOS)
for r in rows:
    print(r["scenario_id"], r["label"], "| next:", r["next_query"], "|", len(json.loads(r["events"])), "events")

# COMMAND ----------

# DBTITLE 1,Checks: enough scenarios, mixed, and every one replays into valid Kafka payloads
assert len(rows) == N, f"only {len(rows)}/{N} scenarios found"
assert {bool(r["user_id"]) for r in rows} == {True, False}, "need both logged-in and guest scenarios"
now = datetime.now(timezone.utc)
for r in rows:
    for e in replay(json.loads(r["events"]), f"check-{r['scenario_id']}", r["user_id"], now):
        event_payload(e)

# COMMAND ----------

# DBTITLE 1,Lakebase synced table (snapshot)
w = WorkspaceClient()
status = sync_snapshot(w, CFG, SCENARIOS, ONLINE, ["scenario_id"])
online = spark.table(ONLINE).count()
assert online == len(rows), f"Lakebase has {online} scenarios, Delta {len(rows)}"
print(ONLINE, status.detailed_state)

summary = {"scenarios": [{k: r[k] for k in ("scenario_id", "label", "next_query")} for r in rows],
           "online_table": ONLINE}
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
