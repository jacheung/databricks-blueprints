# Databricks notebook source
# DBTITLE 1,Declare and materialize the clickstream Feature Views
# MAGIC %md
# MAGIC # Declare and materialize the clickstream Feature Views
# MAGIC
# MAGIC Registers every feature in `features.feature_specs` (generated from config) over `session_events` and
# MAGIC materializes them to the Lakebase online store, where the L2 endpoints look them up:
# MAGIC
# MAGIC | Group | Entity | Window | Trigger | Store |
# MAGIC |---|---|---|---|---|
# MAGIC | session (in-session intent; covers guests) | `session_id` (+ `category` for affinity) | `RollingWindow` (30 min) / latest value | `StreamingMode` from the Kafka Stream (kafka_stream) | online |
# MAGIC | user (long-term, logged-in only) | `user_id` (+ `category` for affinity) | `SlidingWindow` (30 d, 1 d slide) | `CronSchedule` (`feature_store.batch_cron`) | offline + online |
# MAGIC | item (popularity) | `product_id` | `SlidingWindow` (7 d, 1 d slide) | `CronSchedule` | offline + online |
# MAGIC
# MAGIC Then it checks two things the L2 models depend on:
# MAGIC 1. **Point-in-time training values:** `create_training_set` values for sampled searches match a direct count
# MAGIC    over `session_events` of the events strictly before each search: no future leakage, and the search being
# MAGIC    scored isn't counted in its own features.
# MAGIC 2. **Freshness:** per-event lag from a new event in `session_events` to its online feature value, measured
# MAGIC    server-side (`commit_time - event_time` on the online row) over `feature_store.freshness_probes` events.
# MAGIC
# MAGIC Reruns are safe: existing features and materializations are reused. Materializations keep running after this
# MAGIC notebook ends; streaming ones are billable while running.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.16.0" confluent-kafka aws-msk-iam-sasl-signer-python pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone

from pyspark.sql import functions as F

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_data.feature_specs import feature_specs
from rts_shared.config import full_name, load_config, table
from rts_shared.events import event_payload
from rts_shared.kafka import producer

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.feature_engineering.entities import (AggregationFunction, ApproxPercentile, Avg, ColumnSelection, Count,
                                                     CronSchedule, DeltaTableSource, Max, Min, OfflineStoreConfig,
                                                     OnlineStoreConfig, RollingWindow, SlidingWindow, StddevPop,
                                                     StreamSource, StreamingMode)
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
UC, FS = CFG["unity_catalog"], CFG["feature_store"]
SESSION_EVENTS = table(CFG, "session_events")
fe = FeatureEngineeringClient()
# Session features read the Kafka Stream (payload fields under `value.`); user/item features read Delta.
SPECS = feature_specs(CFG, session_prefix="value.")
STREAM = full_name(CFG, CFG["kafka"]["stream"])

# COMMAND ----------

# DBTITLE 1,Online store (shared Lakebase instance)
store = fe.get_online_store(name=FS["online_store"])  # returns None, doesn't raise, when missing
if store is None:
    fe.create_online_store(name=FS["online_store"], capacity=FS["online_capacity"])
for _ in range(30):  # bounded: long blocking polls can outlive a serverless session
    store = fe.get_online_store(name=FS["online_store"])
    if store is not None and str(store.state).upper().endswith("AVAILABLE"):
        break
    time.sleep(30)
print(FS["online_store"], store.state)
fe.get_stream(name=STREAM)  # fails here, not mid-registration, if kafka_stream hasn't run

# COMMAND ----------

# DBTITLE 1,Register features
OPS = {"Count": Count, "Avg": Avg, "Min": Min, "Max": Max, "StddevPop": StddevPop, "ApproxPercentile": ApproxPercentile}


def to_feature(spec):
    if spec.group == "session":
        source = StreamSource(full_name=STREAM, filter_condition=spec.filter)
    else:
        source = DeltaTableSource(catalog_name=UC["catalog"], schema_name=UC["schema"],
                                  table_name=CFG["tables"]["session_events"], filter_condition=spec.filter)
    if spec.op == "ColumnSelection":
        function = ColumnSelection(spec.input)
    else:
        # Session features stream (rolling window to "now"); user/item features are batch sliding windows.
        window = (RollingWindow(window_duration=spec.window) if spec.group == "session"
                  else SlidingWindow(window_duration=spec.window, slide_duration=timedelta(days=1)))
        function = AggregationFunction(OPS[spec.op](input=spec.input, **spec.params), window)
    try:
        return fe.create_feature(source=source, function=function, entity=spec.entity,
                                 timeseries_column=spec.timeseries, catalog_name=UC["catalog"],
                                 schema_name=UC["schema"], name=spec.name)
    except Exception as error:  # definitions persist in UC across runs
        if "already exists" not in str(error).lower():
            raise
        return fe.get_feature(full_name=full_name(CFG, spec.name))


FEATURES = {spec.name: to_feature(spec) for spec in SPECS}
print(f"{len(FEATURES)} features registered")

# COMMAND ----------

# DBTITLE 1,Materialize
def materialize(group, trigger, offline):
    specs = [s for s in SPECS if s.group == group]
    pending = [s for s in specs if not list(fe.list_materialized_features(feature_name=full_name(CFG, s.name)))]
    if pending:
        prefix = f"rts_{group}"
        fe.materialize_features(
            features=[FEATURES[s.name] for s in pending],
            offline_config=OfflineStoreConfig(catalog_name=UC["catalog"], schema_name=UC["schema"],
                                              table_name_prefix=f"{prefix}_offline") if offline else None,
            online_config=OnlineStoreConfig(catalog_name=UC["catalog"], schema_name=UC["schema"],
                                            table_name_prefix=f"{prefix}_online", online_store_name=FS["online_store"]),
            trigger=trigger)
    return {s.name: list(fe.list_materialized_features(feature_name=full_name(CFG, s.name))) for s in specs}


batch_cron = CronSchedule(quartz_cron_expression=FS["batch_cron"], timezone_id="UTC")
MATERIALIZED = {}
MATERIALIZED.update(materialize("session", StreamingMode(), offline=False))
MATERIALIZED.update(materialize("user", batch_cron, offline=True))
MATERIALIZED.update(materialize("item", batch_cron, offline=True))

pipelines = {}
for name, mats in MATERIALIZED.items():
    for m in mats:
        pipelines.setdefault(m.pipeline_id, set()).add(m.table_name)
print(f"{len(pipelines)} pipelines for {len(MATERIALIZED)} features")
for pipeline_id, tables in pipelines.items():
    print(pipeline_id, sorted(tables))

# COMMAND ----------

# DBTITLE 1,Check 1: point-in-time training values match a direct count
# For sampled searches, the training-set value of each session feature must equal what session_events said
# at that moment: no events from after the search may leak in.
# `category` is the composite lookup key for sess_cat_cnt_30m (here the query's category, as for a candidate).
sample = (spark.table(table(CFG, "qac_impressions")).where("split = 'train'")
          .orderBy(F.sha2(F.col("search_id"), 256)).limit(200)
          .select("search_id", "session_id", "category", "event_time", F.col("outcome").alias("label")))
checked = ["sess_n_searches_30m", "sess_n_clicks_30m", "sess_cat_cnt_30m", "sess_avg_price_30m", "sess_last_category"]
training = fe.create_training_set(df=sample, features=[FEATURES[n] for n in checked], label="label",
                                  exclude_columns=[]).load_df()

events = spark.table(SESSION_EVENTS).alias("e")
window = F.col("e.event_time").between(F.col("s.event_time") - F.expr("INTERVAL 30 MINUTES"), F.col("s.event_time"))
expected = (sample.alias("s")
            .join(events, (F.col("e.session_id") == F.col("s.session_id")) & window, "left")
            .groupBy("s.search_id")
            .agg(F.count(F.when(F.col("e.event_type") == "search", 1)).alias("exp_searches"),
                 F.count(F.when(F.col("e.event_type") == "click", 1)).alias("exp_clicks"),
                 F.count(F.when(F.col("e.event_type").isin("click", "add_to_cart")
                                & (F.col("e.category") == F.col("s.category")), 1)).alias("exp_cat")))
compare = training.join(expected, "search_id")
# The expected counts include the search itself (at s.event_time). Feature values cover events strictly before
# the label time, so the right answer is one fewer search and the same clicks.
mismatches = compare.where("coalesce(sess_n_searches_30m, 0) != exp_searches - 1 OR "
                           "coalesce(sess_n_clicks_30m, 0) != exp_clicks OR "
                           "coalesce(sess_cat_cnt_30m, 0) != exp_cat").count()
nonzero_cat = compare.where("coalesce(sess_cat_cnt_30m, 0) > 0").count()
pit = {"sampled": compare.count(), "mismatches": mismatches, "nonzero_category_affinity": nonzero_cat}
display(compare.limit(20))
print(pit)
assert pit["sampled"] > 0 and mismatches == 0, f"training values are not point-in-time correct: {pit}"

# COMMAND ----------

# DBTITLE 1,Check 2: freshness from session_events to the online store
# Per-event freshness, measured server-side like the Feature Views NBO accelerator's benchmark: each probe writes
# one click for its own fresh session_id, and its lag is the online row's commit_time - event_time. Polling only
# decides when to read; it is never part of the number. Read before the 30-minute window expires: expiry
# overwrites the row with a timer row (is_timer = true) and the original commit_time is gone.
# Probes are produced to Kafka exactly like the app's live events, so this is the whole event -> online path.
N_PROBES, WARMUP, SPACING_S, SETTLE_S = FS["freshness_probes"], FS["freshness_warmup"], 2, 600
online_table = next(m.table_name for m in MATERIALIZED["sess_n_clicks_30m"] if m.is_online)
run_tag = f"probe-{uuid.uuid4().hex[:8]}"
probes = [f"{run_tag}-{i:03d}" for i in range(N_PROBES)]
kafka = producer(CFG, WorkspaceClient())


def send_probe(session_id, event_id):
    key, value = event_payload({"session_id": session_id, "event_id": event_id,
                                "event_time": datetime.now(timezone.utc), "event_type": "click",
                                "product_id": "probe-product", "category": "Camera & Photo", "price": 2000.0,
                                "device": "desktop"})
    kafka.produce(CFG["kafka"]["topic"], key=key, value=value)
    kafka.flush(30)  # one event at a time, like a shopper's click


def n_landed(session_ids):
    in_list = ", ".join(f"'{s}'" for s in session_ids)
    return spark.sql(f"SELECT count(DISTINCT session_id) FROM {online_table} "
                     f"WHERE session_id IN ({in_list}) AND is_timer = false").first()[0]


# A new streaming pipeline reads from the latest offset and its flows start minutes after it's created, so probes
# sent before then are never seen. Resend one readiness probe until it lands, then measure.
ready = f"{run_tag}-ready"
deadline = time.time() + 15 * 60
attempt = 0
while not n_landed([ready]):
    if time.time() > deadline:
        raise TimeoutError(f"no probe reached {online_table} after 15 min; is the session pipeline running?")
    send_probe(ready, f"{ready}-{attempt:03d}")
    attempt += 1
    time.sleep(30)
print(f"session pipeline is consuming (readiness probe landed after {attempt} sends)")

for session_id in probes:
    send_probe(session_id, f"{session_id}-001")
    time.sleep(SPACING_S)

in_list = ", ".join(f"'{p}'" for p in probes)
deadline = time.time() + SETTLE_S
landed = 0
while time.time() < deadline:
    landed = n_landed(probes)
    if landed >= N_PROBES:
        break
    time.sleep(10)

freshness = spark.sql(f"""
    WITH per_probe AS (
      SELECT session_id, CAST(element_at(split(session_id, '-'), -1) AS INT) AS idx,
             MIN(unix_millis(commit_time) - unix_millis(event_time)) AS lag_ms
      FROM {online_table}
      WHERE session_id IN ({in_list}) AND is_timer = false
      GROUP BY session_id)
    SELECT count(*) AS n, percentile_approx(lag_ms, 0.5) AS p50_ms, percentile_approx(lag_ms, 0.95) AS p95_ms,
           percentile_approx(lag_ms, 0.99) AS p99_ms, min(lag_ms) AS min_ms, max(lag_ms) AS max_ms,
           count_if(lag_ms < 1000) AS under_1s, count_if(lag_ms < 200) AS under_200ms
    FROM per_probe WHERE idx >= {WARMUP}""").first().asDict()
freshness |= {"online_table": online_table, "probes_written": N_PROBES, "landed": landed, "warmup_discarded": WARMUP}
print(freshness)
assert landed == N_PROBES, f"only {landed}/{N_PROBES} probes reached the online store: {freshness}"
assert freshness["p95_ms"] < 1000, f"p95 event -> online freshness is not under 1 s: {freshness}"

# COMMAND ----------

# DBTITLE 1,Summary
summary = {
    "features": len(FEATURES),
    "by_group": {g: sum(s.group == g for s in SPECS) for g in ("session", "user", "item")},
    "pipelines": len(pipelines),
    "point_in_time": pit,
    "freshness": freshness,
}
print(json.dumps(summary, indent=2, default=str))
dbutils.notebook.exit(json.dumps(summary, default=str))
