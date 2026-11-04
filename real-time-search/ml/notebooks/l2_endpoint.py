# Databricks notebook source
# DBTITLE 1,L2: serving endpoint (QAC or SERP)
# MAGIC %md
# MAGIC # L2: serving endpoint (QAC or SERP)
# MAGIC
# MAGIC Serves the latest version of `models.<model_key>.registered_name` on `models.<model_key>.endpoint`
# MAGIC (route-optimized, workload type and size from config). `variant=lgbm` serves the SERP LightGBM comparator from
# MAGIC `models.serp_l2.lgbm` instead (CPU, for the latency comparison). The models were logged with `fe.log_model`, so
# MAGIC the endpoint looks up the session (Kafka stream), user and item (Delta) features in the online store itself;
# MAGIC callers send only keys and the request fields (`rts_shared.retrieval.qac_l2_records` / `serp_l2_records`).
# MAGIC
# MAGIC Check: the endpoint is ready and serving that version. Queries are checked elsewhere: route-optimized endpoints
# MAGIC accept only OAuth tokens, which notebook credentials can't provide. The app (its service principal uses OAuth)
# MAGIC and a local SDK client (CLI OAuth login) can query it.
# MAGIC
# MAGIC Reruns are safe: an endpoint already serving the latest version is reused. The endpoint bills while it exists
# MAGIC (`scale_to_zero_enabled` from config).
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

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.serving import ensure_endpoint, wait_ready
from rts_shared.config import full_name, load_config, model_name

from databricks.sdk import WorkspaceClient
from databricks.sdk.service.serving import ServedEntityInput, ServingModelWorkloadType

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
dbutils.widgets.dropdown("model_key", "qac_l2", ["qac_l2", "serp_l2"])
dbutils.widgets.dropdown("variant", "", ["", "lgbm"])  # lgbm: the SERP LightGBM comparator
CFG = load_config(dbutils.widgets.get("config_path"))
KEY, VARIANT = dbutils.widgets.get("model_key"), dbutils.widgets.get("variant")
L2 = CFG["models"][KEY]
MODEL_NAME = model_name(CFG, KEY)
if VARIANT:
    assert KEY == "serp_l2", f"variant {VARIANT!r} exists only for serp_l2"
    L2 = {**L2, **L2[VARIANT]}  # its own name, endpoint and workload; route optimization etc. from serp_l2
    MODEL_NAME = full_name(CFG, L2["registered_name"])
ENDPOINT = L2["endpoint"]
w = WorkspaceClient()

# COMMAND ----------

# DBTITLE 1,Create or update the endpoint (latest model version)
version = str(max(int(v.version) for v in w.model_versions.list(full_name=MODEL_NAME)))
entity = ServedEntityInput(entity_name=MODEL_NAME, entity_version=version, workload_size=L2["workload_size"],
                           workload_type=ServingModelWorkloadType(L2["workload_type"]),
                           scale_to_zero_enabled=L2["scale_to_zero_enabled"])
print(ENDPOINT, ensure_endpoint(w, ENDPOINT, entity, route_optimized=L2["route_optimized"]))

# COMMAND ----------

# DBTITLE 1,Check: ready and serving the latest version
endpoint = wait_ready(w, ENDPOINT)  # model + feature-lookup images can take 20+ minutes to build
served = [(s.entity_name, s.entity_version) for s in endpoint.config.served_entities]
assert served == [(MODEL_NAME, version)], f"{ENDPOINT} serves {served}, expected {MODEL_NAME} v{version}"

summary = {"endpoint": ENDPOINT, "model": MODEL_NAME, "version": version, "route_optimized": endpoint.route_optimized,
           "url": endpoint.endpoint_url}
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
