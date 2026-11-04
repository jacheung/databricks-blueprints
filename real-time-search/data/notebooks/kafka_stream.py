# Databricks notebook source
# DBTITLE 1,Stream session events from Kafka into Feature Engineering
# MAGIC %md
# MAGIC # Stream session events from Kafka into Feature Engineering
# MAGIC
# MAGIC Session features need sub-second freshness. On a Delta source they lag by minutes (Delta commits are polled
# MAGIC in micro-batches), so session events go through Kafka (Amazon MSK) instead, where Feature Engineering's
# MAGIC managed Stream processes them record by record. User and item features stay on Delta.
# MAGIC
# MAGIC This notebook, all steps idempotent:
# MAGIC 1. Creates the UC Kafka connection (`kafka.connection`) to the MSK brokers, authenticated with the UC service
# MAGIC    credential (`kafka.service_credential`), and the topic (`kafka.topic`).
# MAGIC 2. Creates the Feature Engineering **Stream** (`kafka.stream`). Its managed ingestion pipeline copies the topic
# MAGIC    into `session_events_ingest`, the training history for the streaming session features in feature_views.
# MAGIC 3. Waits until the ingestion pipeline is RUNNING. It reads from the latest offset, so anything produced
# MAGIC    before it runs is never seen.
# MAGIC 4. Replays the simulated `session_events` history into the topic with original `event_time`s, and waits until
# MAGIC    the ingestion table has it.
# MAGIC
# MAGIC Run after clickstream_sim and before feature_views (the `clickstream_job` task order).
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

from pyspark.sql import functions as F

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_shared.config import full_name, load_config, table
from rts_shared.events import EVENT_FIELDS, payload_json_schema
from rts_shared.kafka import ensure_topic, spark_sink_options

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.feature_engineering.entities import (DirectSchemas, IngestionConfig, IngestionDestination,
                                                     KafkaStreamConfig, KafkaSubscriptionMode, SchemaConfig,
                                                     StreamConnectionConfig)
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
K = CFG["kafka"]
STREAM, INGEST, SESSION_EVENTS = full_name(CFG, K["stream"]), table(CFG, "session_events_ingest"), table(CFG, "session_events")
w, fe = WorkspaceClient(), FeatureEngineeringClient()
spark.conf.set("spark.sql.session.timeZone", "UTC")

# COMMAND ----------

# DBTITLE 1,Kafka connection and topic
try:
    w.connections.get(K["connection"])
    print(f"connection {K['connection']} exists")
except NotFound:
    w.api_client.do("POST", "/api/2.1/unity-catalog/connections", body={
        "name": K["connection"], "connection_type": "KAFKA",
        "options": {"bootstrap_servers": K["bootstrap_servers"], "credential": K["service_credential"]},
        "comment": "real-time-search session events"})
    print(f"created connection {K['connection']}")
print(f"topic {K['topic']}: {'created' if ensure_topic(CFG, w) else 'exists'}")

# COMMAND ----------

# DBTITLE 1,Feature Engineering Stream and its ingestion pipeline
try:
    stream = fe.get_stream(name=STREAM)
    print(f"stream {STREAM} exists")
except Exception:
    stream = fe.create_stream(
        name=STREAM,
        source_config=KafkaStreamConfig(subscription_mode=KafkaSubscriptionMode(subscribe=K["topic"])),
        connection_config=StreamConnectionConfig(uc_connection_name=K["connection"]),
        schema_config=DirectSchemas(payload_schema=SchemaConfig(json_schema=payload_json_schema())),
        ingestion_config=IngestionConfig(ingestion_destination=IngestionDestination(delta_table_name=INGEST),
                                         deduplication_columns=["value.event_id"]))
    print(f"created stream {STREAM}")

pipeline_id = fe.get_stream(name=STREAM).ingestion_config.ingestion_pipeline_id
deadline = time.time() + 15 * 60  # first start takes ~5-7 min
while True:
    state = str(w.pipelines.get(pipeline_id=pipeline_id).state)
    if "RUNNING" in state:
        break
    if "FAILED" in state:
        raise RuntimeError(f"ingestion pipeline {pipeline_id} is {state}; check its event log")
    if "IDLE" in state:  # doesn't always auto-start
        w.pipelines.start_update(pipeline_id=pipeline_id)
    if time.time() > deadline:
        raise TimeoutError(f"ingestion pipeline {pipeline_id} not RUNNING after 15 min ({state})")
    time.sleep(20)
print(f"ingestion pipeline {pipeline_id} RUNNING")

# COMMAND ----------

# DBTITLE 1,Replay the simulated history into the topic
def ingested_event_ids():
    # The ingestion table is registered before its Delta path exists, so treat any read error as "empty".
    # spark.table() is lazy on Spark Connect, so force a read inside the try or a missing table raises later.
    try:
        ids = spark.table(INGEST).select(F.col("value.event_id").alias("event_id")).distinct()
        ids.limit(1).collect()
        return ids
    except Exception:
        return None


history = spark.table(SESSION_EVENTS)
seen = ingested_event_ids()
pending = history if seen is None else history.join(seen, "event_id", "left_anti")
n_pending, n_history = pending.count(), history.count()
if n_pending:
    payload = F.to_json(F.struct(*[F.date_format(n, "yyyy-MM-dd'T'HH:mm:ss.SSS'Z'").alias(n) if t == "timestamp"
                                   else F.col(n) for n, t in EVENT_FIELDS]))
    (pending.select(F.col("session_id").alias("key"), payload.alias("value"))
     .write.format("kafka").options(**spark_sink_options(CFG)).save())
print(f"replayed {n_pending:,} of {n_history:,} history events")

deadline = time.time() + 20 * 60
while True:
    seen = ingested_event_ids()
    n_ingested = seen.count() if seen is not None else 0
    if n_ingested >= n_history or time.time() > deadline:
        break
    print(f"ingested {n_ingested:,} / {n_history:,}")
    time.sleep(30)

# COMMAND ----------

# DBTITLE 1,Summary
summary = {"stream": STREAM, "ingestion_table": INGEST, "ingestion_pipeline_id": pipeline_id, "topic": K["topic"],
           "replayed": n_pending, "history": n_history, "ingested": n_ingested}
print(json.dumps(summary, indent=2))
assert n_ingested >= n_history, f"ingestion table has {n_ingested:,} of {n_history:,} history events"
dbutils.notebook.exit(json.dumps(summary))
