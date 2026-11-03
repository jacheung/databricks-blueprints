# Databricks notebook source
# DBTITLE 1,Tear down the clickstream features and the Kafka Stream
# MAGIC %md
# MAGIC # Tear down the clickstream features and the Kafka Stream
# MAGIC
# MAGIC Deletes, in this order, everything `kafka_stream` and `feature_views` create from config:
# MAGIC 1. Each feature's materializations (offline and online), then the feature.
# MAGIC 2. The Feature Engineering Stream (`kafka.stream`).
# MAGIC
# MAGIC Run it before `clickstream_job` with `rebuild_data=true`: rebuilding replaces `session_events`, which breaks
# MAGIC live materializations reading it. Tables, the online store, the Kafka connection and the topic are kept.
# MAGIC
# MAGIC With `confirm=no` (the default) it only lists what it would delete.
# MAGIC
# MAGIC **Compute:** Serverless, environment version 4+.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.16.0" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import sys

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/data/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/data/src"]
from rts_data.feature_specs import feature_specs
from rts_shared.config import full_name, load_config

from databricks.feature_engineering import FeatureEngineeringClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
dbutils.widgets.dropdown("confirm", "no", ["no", "yes"])  # yes = delete; no = list only
CFG = load_config(dbutils.widgets.get("config_path"))
DELETE = dbutils.widgets.get("confirm") == "yes"
STREAM = full_name(CFG, CFG["kafka"]["stream"])
fe = FeatureEngineeringClient()

# COMMAND ----------

# DBTITLE 1,Features: materializations first, then the feature
# A feature with a live materialization strands it, so materializations go first. Batch features have an
# offline + online pair: deleting the offline one removes the online one too, and the online one can't be deleted
# on its own. Streaming features are online-only.
result = {"confirm": DELETE, "features": [], "materializations": 0, "stream": None, "errors": []}
for spec in feature_specs(CFG):
    name = full_name(CFG, spec.name)
    try:
        fe.get_feature(full_name=name)
    except Exception:
        continue
    result["features"].append(spec.name)
    mats = list(fe.list_materialized_features(feature_name=name))
    offline = [m for m in mats if not m.is_online]
    result["materializations"] += len(offline or mats)
    if not DELETE:
        continue
    try:
        for m in offline or mats:
            fe.delete_materialized_feature(materialized_feature=m)
        assert not list(fe.list_materialized_features(feature_name=name)), "materializations left"
        fe.delete_feature(full_name=name)
    except Exception as error:
        result["errors"].append(f"{spec.name}: {type(error).__name__}: {str(error)[:200]}")

# COMMAND ----------

# DBTITLE 1,Stream
try:
    fe.get_stream(name=STREAM)
    result["stream"] = STREAM
    if DELETE:
        fe.delete_stream(name=STREAM)
except Exception as error:
    if result["stream"]:  # it exists but the delete failed
        result["errors"].append(f"stream: {type(error).__name__}: {str(error)[:200]}")

# COMMAND ----------

# DBTITLE 1,Summary
print(json.dumps(result, indent=2))
assert not result["errors"], f"teardown errors: {result['errors']}"
dbutils.notebook.exit(json.dumps(result))
