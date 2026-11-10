# Databricks notebook source
# DBTITLE 1,Shift the simulated history so it ends yesterday
# MAGIC %md
# MAGIC # Shift the simulated history so it ends yesterday
# MAGIC
# MAGIC The user (30 d) and item (7 d) `SlidingWindow`s end at the latest cron run, but the simulated history stops on the
# MAGIC day before the `clickstream_sim` run, so online user/item features fade day by day (the stale batch features
# MAGIC gotcha in `data/PLAN.md`). This notebook moves `session_events.event_time` forward by whole days
# MAGIC (`rts_shared.config.shift_days`), so the history covers the same dates a fresh `clickstream_sim` run would. It then
# MAGIC runs the user/item batch materialization jobs and checks the latest windows are back to full strength.
# MAGIC
# MAGIC Safe to update in place: each materialization run reads `session_events` as a batch and rewrites its trailing
# MAGIC window-end dates (`replaceWhere`), not the change feed. Reruns are safe: the shift is 0 when already current.
# MAGIC
# MAGIC **Only `session_events` moves.** Labels (`qac_impressions`, `serp_impressions`, `sim_sessions`) and the Kafka copy
# MAGIC (`session_events_ingest`) keep their dates, so after a shift **don't retrain**: training-set user/item features
# MAGIC would be a shift behind their labels. Rebuild first (`teardown_job`, then `clickstream_job` with
# MAGIC `rebuild_data=true`).
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.16.0" "databricks-sdk>=0.81" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys
from datetime import date, timedelta

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_data.feature_specs import feature_specs
from rts_shared.config import full_name, load_config, shift_days, table, window_strength

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
SESSION_EVENTS = table(CFG, "session_events")
BATCH = [s for s in feature_specs(CFG) if s.group in ("user", "item")]
fe, w = FeatureEngineeringClient(), WorkspaceClient()

# COMMAND ----------

# DBTITLE 1,Shift session_events by whole days
first = spark.sql(f"SELECT MIN(DATE(event_time)) AS d FROM {SESSION_EVENTS}").first()["d"]
DAYS = shift_days(CFG, first_event_date=first, today=date.today())
if DAYS:
    spark.sql(f"UPDATE {SESSION_EVENTS} SET event_time = event_time + INTERVAL {DAYS} DAYS")
dates = spark.sql(f"SELECT MIN(event_time) AS lo, MAX(event_time) AS hi FROM {SESSION_EVENTS}").first()
print(f"shifted {DAYS} days; history now {dates['lo']} .. {dates['hi']}")

# COMMAND ----------

# DBTITLE 1,Rerun the user/item batch materializations
# One Feature Store-managed job per materialization batch refreshes its offline table and online copy. Wait out a
# running one first: it may have read the history before the shift, and a second run can't start alongside it.
offline = {}  # feature name -> offline table
jobs = set()
for spec in BATCH:
    for m in fe.list_materialized_features(feature_name=full_name(CFG, spec.name)):
        if not m.is_online:
            offline[spec.name] = m.table_name
            mat = w.api_client.do("GET", f"/api/2.0/feature-engineering/materialized-features/{m.materialized_feature_id}")
            jobs.add(mat["job_id"])
assert len(offline) == len(BATCH), f"offline materializations for {sorted(offline)} only"

for job_id in jobs:
    for run in w.jobs.list_runs(job_id=job_id, active_only=True):
        w.jobs.wait_get_run_job_terminated_or_skipped(run_id=run.run_id, timeout=timedelta(minutes=30))
waiters = [w.jobs.run_now(job_id=job_id) for job_id in jobs]
results = [r.result(timeout=timedelta(minutes=30)) for r in waiters]
states = {r.job_id: str(r.state.result_state) for r in results}
print(states)
assert all(s.endswith("SUCCESS") for s in states.values()), states

# COMMAND ----------

# DBTITLE 1,Check: the latest windows are back to full strength
# Count features sum to about the same total in every full window; a stale history shows as totals falling towards the
# latest window-end date. The latest must be within 5% of the best day.
strength = {}
for spec in BATCH:
    if spec.op != "Count":
        continue
    daily = spark.sql(f"SELECT DATE(event_time) AS d, SUM({spec.name}) AS s FROM {offline[spec.name]} GROUP BY 1").collect()
    strength[spec.name] = window_strength([(r["d"], float(r["s"])) for r in daily])
print(json.dumps(strength, indent=2))
weak = {k: v for k, v in strength.items() if v["ratio"] < 0.95}
assert not weak, f"latest windows below 95% of the best day: {weak}"

summary = {"shift_days": DAYS, "history": [str(dates["lo"]), str(dates["hi"])], "jobs": states, "strength": strength}
dbutils.notebook.exit(json.dumps(summary))
