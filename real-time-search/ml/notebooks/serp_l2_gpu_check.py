# Databricks notebook source
# DBTITLE 1,SERP L2: AI Runtime check (UCDataset, Feature Engineering, CUDA scoring)
# MAGIC %md
# MAGIC # SERP L2: AI Runtime check
# MAGIC
# MAGIC Runs on the environment the DCNv2 trains on (Serverless GPU, AI environment `models.serp_l2.ai_env`, one A10),
# MAGIC before the training notebook is written, to settle what it can rely on:
# MAGIC
# MAGIC | Check | Needed? | If it fails |
# MAGIC |---|---|---|
# MAGIC | `databricks-feature-engineering` installs and imports | yes (`fe.log_model` runs here) | assert |
# MAGIC | CUDA is available; the `SerpRanker` pyfunc scores the same on CUDA as on CPU | yes | assert |
# MAGIC | `databricks-uc-dataset` installs | nice to have | reported; training reads the table into memory |
# MAGIC | UCDataset reads fixed-length `array<float>` columns as `[batch, length]` tensors | nice to have | reported |
# MAGIC
# MAGIC Writes and drops a small scratch table (`serp_l2_gpu_check`). No endpoint.
# MAGIC
# MAGIC **Compute:** Serverless GPU (A10), AI environment v6.

# COMMAND ----------

# DBTITLE 1,Install dependencies (Feature Engineering is required; UCDataset is tried below)
# MAGIC %pip install "databricks-feature-engineering>=0.18.2" pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import importlib
import json
import os
import subprocess
import sys

import numpy as np
import torch

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src", f"{ROOT}/ml/tests"]
from rts_shared.config import full_name, load_config

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
CFG = load_config(dbutils.widgets.get("config_path"))
results = {"python": sys.version.split()[0], "torch": torch.__version__}

# COMMAND ----------

# DBTITLE 1,Check 1: Feature Engineering client imports
from databricks.feature_engineering import FeatureEngineeringClient
from importlib.metadata import version

FeatureEngineeringClient()
results["feature_engineering"] = version("databricks-feature-engineering")

# COMMAND ----------

# DBTITLE 1,Check 2: CUDA, and the pyfunc scores the same on GPU as on CPU
import mlflow

from rts_ml.serp_l2 import MODEL_COLUMNS
from rts_ml.serp_l2_dcn import DCNv2, score
from rts_ml.serp_l2_model import SerpRanker
from serp_l2_data import searches  # the synthetic candidate rows the unit tests use

assert torch.cuda.is_available(), "no CUDA device on this task"
results["gpu"] = torch.cuda.get_device_name(0)

torch.manual_seed(0)
model = DCNv2(n_features=len(MODEL_COLUMNS), mean=np.zeros(len(MODEL_COLUMNS)), std=np.ones(len(MODEL_COLUMNS))).eval()
rows = searches(20, seed=0).drop(columns=["session_id", "user_id", "product_id"])  # keyless, like the endpoint
cpu_scores = score(model, rows)
ranker = SerpRanker(model)
ranker.load_context(None)  # moves the module to CUDA when there is one
assert next(ranker.model.parameters()).is_cuda, "SerpRanker.load_context did not move the model to CUDA"
gpu_scores = np.asarray(ranker.predict(None, rows))
results["cpu_vs_gpu_max_abs_diff"] = float(np.abs(gpu_scores - cpu_scores).max())
assert results["cpu_vs_gpu_max_abs_diff"] < 1e-4, f"GPU scores differ from CPU: {results['cpu_vs_gpu_max_abs_diff']}"

# COMMAND ----------

# DBTITLE 1,Check 3 (nice to have): install UCDataset
install = subprocess.run([sys.executable, "-m", "pip", "install", "-q", "databricks-uc-dataset==0.0.1b1"],
                         capture_output=True, text=True, cwd="/tmp")  # the task's own cwd may not exist
results["uc_dataset_install"] = "ok" if install.returncode == 0 else (install.stderr or install.stdout)[-600:]
print(results["uc_dataset_install"])

# COMMAND ----------

# DBTITLE 1,Check 4 (nice to have): UCDataset reads fixed-length array columns
# One row per search, like the training table will be: x [L * F] flattened, label [L], mask [L].
L, F, N = CFG["models"]["serp_l2"]["candidates"], len(MODEL_COLUMNS), 64
SCRATCH = full_name(CFG, "serp_l2_gpu_check")
if results["uc_dataset_install"] == "ok":
    rng = np.random.default_rng(0)
    data = [(f"g{i}", "train" if i < 48 else "valid", rng.random(L * F).astype(float).tolist(),
             rng.integers(0, 3, L).astype(float).tolist(), [1.0] * L) for i in range(N)]
    (spark.createDataFrame(data, "group_id string, split string, x array<float>, label array<float>, "
                                 "mask array<float>")
     .write.mode("overwrite").saveAsTable(SCRATCH))
    try:
        importlib.invalidate_caches()
        from databricks.uc_dataset.beta import UCDataset
        from databricks.uc_dataset.beta.dataframe import col, table as uc_table
        from torch.utils.data import DataLoader

        os.environ.setdefault("RANK", "0")        # UCDataset splits rows by rank; single GPU = rank 0 of 1
        os.environ.setdefault("WORLD_SIZE", "1")
        df = uc_table(SCRATCH).filter(col("split") == "train").select("x", "label", "mask")
        batches = list(DataLoader(UCDataset(df=df, batch_size=16), batch_size=None))
        shapes = {k: list(v.shape) for k, v in batches[0].items()}
        results["uc_dataset_arrays"] = {"batches": len(batches), "rows": sum(len(b["label"]) for b in batches),
                                        "first_batch_shapes": shapes,
                                        "dtypes": {k: str(v.dtype) for k, v in batches[0].items()}}
        ok = (shapes.get("x") == [16, L * F] and shapes.get("label") == [16, L]
              and results["uc_dataset_arrays"]["rows"] == 48)
        results["uc_dataset_arrays"]["ok"] = ok
    except Exception as error:
        results["uc_dataset_arrays"] = {"ok": False, "error": f"{type(error).__name__}: {str(error)[:600]}"}
    finally:
        spark.sql(f"DROP TABLE IF EXISTS {SCRATCH}")
else:
    results["uc_dataset_arrays"] = {"ok": False, "error": "not installed"}
print(json.dumps(results["uc_dataset_arrays"], indent=2))

# COMMAND ----------

# DBTITLE 1,Summary
results["training_reads"] = "UCDataset" if results["uc_dataset_arrays"].get("ok") else "in memory (toPandas)"
print(json.dumps(results, indent=2))
dbutils.notebook.exit(json.dumps(results))
