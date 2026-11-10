# Databricks notebook source
# DBTITLE 1,SERP L2: DCNv2 on Serverless GPU, LightGBM comparator, registered pyfunc
# MAGIC %md
# MAGIC # SERP L2: DCNv2 on Serverless GPU
# MAGIC
# MAGIC Reads `serp_l2_matrix` (one row per search: L1 top `models.serp_l2.candidates` × the model features, built by
# MAGIC `serp_l2_training_set`) into memory: ~15K searches is a few hundred MB, so one A10 and no DDP or streaming loader.
# MAGIC
# MAGIC 1. **DCNv2** (`rts_ml.serp_l2_dcn`): 3 cross layers ‖ MLP 128-64, listwise softmax loss per search, Adam on the
# MAGIC    GPU, early-stopped on validation NDCG@10.
# MAGIC 2. **LightGBM LambdaRank** on the same matrix, as a comparator. Registered (`models.serp_l2.lgbm`) only so its
# MAGIC    CPU endpoint's latency can be compared with DCNv2's GPU endpoint.
# MAGIC 3. **Test split:** NDCG@10 of L1 order vs LightGBM vs DCNv2, paired bootstrap 95% CIs, overall and per slice
# MAGIC    (first vs 2nd+ search, guest, intent). Searches whose engaged product isn't among the candidates have no NDCG
# MAGIC    and are left out for every scorer (the ceiling is reported).
# MAGIC 4. **`fe.log_model`** as pyfuncs (`rts_ml.serp_l2_model`: crosses + DCNv2, crosses + LightGBM) registered to
# MAGIC    `models.serp_l2.registered_name` and `models.serp_l2.lgbm.registered_name`, with the `TrainingSet` rebuilt
# MAGIC    from `serp_l2_spine` (same features).
# MAGIC
# MAGIC Checks: raw Feature View rows scored through the logged model match the packed matrix scores (training and
# MAGIC serving read the same inputs), and both registered models reload and score them the same. Latency is measured
# MAGIC against the endpoints (`ml/scripts/bench_serp_l2.py`). Lift is reported, not asserted.
# MAGIC
# MAGIC **Compute:** Serverless GPU (one A10), AI environment v6 (`models.serp_l2.ai_env`).

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "databricks-feature-engineering>=0.18.2" lightgbm pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import posixpath
import sys
import time

import lightgbm
import mlflow
import numpy as np
import pandas as pd
import torch
from pyspark.sql import functions as F

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src", f"{ROOT}/ml/src"]
from rts_ml.qac_l2 import fit_ranker, slice_report
from rts_ml.serp_l2 import MODEL_COLUMNS, model_matrix, ndcg_per_group
from rts_ml.serp_l2_dcn import fit_dcn, packed_ndcg, score
from rts_ml.serp_l2_model import SerpLgbmRanker, SerpRanker
from rts_shared.config import full_name, load_config, model_name, resolve_auto
from rts_shared.features import feature_keys
from rts_shared.retrieval import SERP_INDEX_PK, SERP_L2_REQUEST_COLUMNS

from databricks.feature_engineering import FeatureEngineeringClient
from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
w = WorkspaceClient()
CFG = resolve_auto(load_config(dbutils.widgets.get("config_path")), w.current_user.me().user_name)
L2 = CFG["models"]["serp_l2"]
N, F_ = L2["candidates"], len(MODEL_COLUMNS)
MODEL_NAME = model_name(CFG, "serp_l2")
SPINE, MATRIX = full_name(CFG, "serp_l2_spine"), full_name(CFG, "serp_l2_matrix")
assert torch.cuda.is_available(), "no CUDA device on this task"
DEVICE = "cuda"
fe = FeatureEngineeringClient()
torch.manual_seed(0)

# COMMAND ----------

# DBTITLE 1,Load the packed matrix (one row per search)
SLICES = ["search", "guest", "intent"]


def load(split):
    pdf = spark.table(MATRIX).where(F.col("split") == split).orderBy("search_id").toPandas()
    x = np.stack([np.asarray(v, np.float32) for v in pdf["x"]]).reshape(len(pdf), N, F_)
    y = np.stack([np.asarray(v, np.float32) for v in pdf["label"]])
    mask = np.stack([np.asarray(v, np.float32) for v in pdf["mask"]])
    return pdf[["search_id", *SLICES]], torch.from_numpy(x), torch.from_numpy(y), torch.from_numpy(mask)


train_meta, x_tr, y_tr, m_tr = load("train")
valid_meta, x_va, y_va, m_va = load("valid")
test_meta, x_te, y_te, m_te = load("test")
print({s: len(m) for s, m in (("train", train_meta), ("valid", valid_meta), ("test", test_meta))},
      f"{x_tr.element_size() * (x_tr.nelement() + x_va.nelement() + x_te.nelement()) / 1e9:.2f} GB of features")

# Standardization from real (unpadded) training candidates.
real = x_tr[m_tr > 0]
MEAN, STD = real.mean(0).numpy(), real.std(0).numpy()
constant = [c for c, s in zip(MODEL_COLUMNS, STD) if s < 1e-6]
print("constant in train:", constant)
assert not constant, f"features constant in training would be unseen variation at serving: {constant}"

# COMMAND ----------

# DBTITLE 1,Train DCNv2 on the GPU
BATCH = 64  # searches per step
x_gpu, y_gpu, m_gpu = x_tr.to(DEVICE), y_tr.to(DEVICE), m_tr.to(DEVICE)
gen = torch.Generator(device="cpu").manual_seed(0)


def train_batches():  # a fresh shuffle every epoch
    order = torch.randperm(len(x_gpu), generator=gen)
    return [(x_gpu[i], y_gpu[i], m_gpu[i]) for i in order.split(BATCH)]


history = []
t0 = time.time()
model = fit_dcn(train_batches, (x_va, y_va, m_va), mean=MEAN, std=STD, max_epochs=40, patience=5, device=DEVICE,
                log=lambda h: (history.append(h), print(h)))
train_seconds = round(time.time() - t0, 1)
print(f"best epoch {model.best_epoch}, valid NDCG@10 {model.best_valid_ndcg10:.4f}, {train_seconds} s")

# COMMAND ----------

# DBTITLE 1,LightGBM LambdaRank comparator on the same matrix
def rows(meta, x, y, mask):
    """Packed searches -> candidate rows (unpadded) with the matrix columns, label, L1 position and slices."""
    g, pos = torch.nonzero(mask > 0, as_tuple=True)
    out = pd.DataFrame(x[g, pos].numpy(), columns=list(MODEL_COLUMNS))
    out["group_id"], out["label"], out["l1"] = meta["search_id"].values[g.numpy()], y[g, pos].numpy().astype(int), \
        -(pos.numpy() + 1)
    for s in SLICES:
        out[s] = meta[s].values[g.numpy()]
    return out


as_matrix = lambda df: df[list(MODEL_COLUMNS)]
train_rows, valid_rows = rows(train_meta, x_tr, y_tr, m_tr), rows(valid_meta, x_va, y_va, m_va)
has_positive = lambda df: df[df.groupby("group_id")["label"].transform("max") > 0]  # groups with no positive teach nothing
t0 = time.time()
ranker = fit_ranker(has_positive(train_rows), has_positive(valid_rows), matrix=as_matrix, eval_at=10)
lgbm_seconds = round(time.time() - t0, 1)
print(f"LightGBM best iteration {ranker.best_iteration_}, {lgbm_seconds} s")
del train_rows, valid_rows

# COMMAND ----------

# DBTITLE 1,Evaluate on the test split: L1 vs LightGBM vs DCNv2
test = rows(test_meta, x_te, y_te, m_te)
test["lgbm"] = ranker.predict(as_matrix(test))
model_gpu = model.to(DEVICE)
with torch.no_grad():
    dcn_packed = torch.cat([model_gpu(x_te[i:i + 256].to(DEVICE)).cpu() for i in range(0, len(x_te), 256)])
g, pos = torch.nonzero(m_te > 0, as_tuple=True)
test["dcn"] = dcn_packed[g, pos].numpy()
model = model_gpu.cpu()

per = lambda **cols: slice_report(test.assign(**cols), SLICES, per_group=ndcg_per_group, metrics=("ndcg",))
# JSON round trip: slice values come back as numpy scalars, which MLflow and the exit payload can't serialize.
as_json = lambda r: json.loads(json.dumps(r, default=lambda o: o.item()))
report = {"lgbm_vs_l1": as_json(per(l2=test["lgbm"])), "dcn_vs_l1": as_json(per(l2=test["dcn"])),
          "dcn_vs_lgbm": as_json(per(l1=test["lgbm"], l2=test["dcn"]))}
table_ = pd.DataFrame([{"comparison": k, **r} for k, rs in report.items() for r in rs]).astype({"value": str})
display(table_)
overall = {k: next(r for r in rs if r["slice"] == "all") for k, rs in report.items()}
ceiling = {"test_searches": len(test_meta), "with_engaged_candidate": int(test.groupby("group_id")["label"].max()
                                                                         .gt(0).sum())}
print(json.dumps({k: {m: round(v[m], 4) for m in ("n", "l1", "l2", "lift", "lift_lo", "lift_hi")}
                  for k, v in overall.items()}, indent=2), ceiling)
assert abs(packed_ndcg(dcn_packed, y_te, m_te) - overall["dcn_vs_l1"]["l2"]) < 1e-6  # one NDCG implementation

# COMMAND ----------

# DBTITLE 1,TrainingSet for fe.log_model (same features as serp_l2_training_set)
FEATURES = [fe.get_feature(full_name=full_name(CFG, n)) for n in [*feature_keys(CFG), *SERP_L2_REQUEST_COLUMNS]]
spine = spark.table(SPINE)
training_set = fe.create_training_set(df=spine, features=FEATURES, label="label", exclude_columns=[])

# Raw Feature View rows for a few test searches, as the endpoint will see them (lookup keys stripped).
check_ids = test_meta["search_id"].head(5).tolist()
raw = (fe.create_training_set(df=spine.where(F.col("search_id").isin(check_ids)), features=FEATURES, label="label",
                              exclude_columns=[]).load_df().toPandas().sort_values(["search_id", "l1_rank"]))
keyless = raw.drop(columns=["session_id", "user_id", SERP_INDEX_PK, "label"])
packed_scores = test.set_index(["group_id", "l1"])["dcn"]
expected = np.array([packed_scores[(s, -r)] for s, r in zip(raw["search_id"], raw["l1_rank"])])
diff_raw = float(np.abs(score(model, keyless) - expected).max())
print(f"raw rows vs packed matrix, max |score diff| = {diff_raw:.2e} over {len(raw)} rows")
assert diff_raw < 1e-4, "the raw-row path (serving) scores differently from the packed matrix (training)"

# COMMAND ----------

# DBTITLE 1,Log and register both models (fe.log_model, pyfunc)
experiment = CFG["mlflow"]["experiment"]
w.workspace.mkdirs(posixpath.dirname(experiment))
mlflow.set_experiment(experiment)
mlflow.set_registry_uri("databricks-uc")
LGBM_NAME = full_name(CFG, L2["lgbm"]["registered_name"])
CODE_PATHS = [f"{ROOT}/ml/src/rts_ml", f"{ROOT}/shared/src/rts_shared"]

with mlflow.start_run(run_name="serp_l2_dcnv2") as run:
    mlflow.log_params({"candidates": N, "features": F_, "batch_searches": BATCH, "best_epoch": model.best_epoch,
                       "lgbm_best_iteration": ranker.best_iteration_, "gpu": torch.cuda.get_device_name(0)})
    for h in history:
        mlflow.log_metrics({"train_loss": h["train_loss"], "valid_ndcg10": h["valid_ndcg10"]}, step=h["epoch"])
    mlflow.log_metrics({f"test_ndcg10_{k}_{side}": overall[k][side] for k in overall for side in ("l1", "l2", "lift")})
    mlflow.log_dict({"report": report, "ceiling": ceiling}, "eval/report.json")
    fe.log_model(model=SerpRanker(model), artifact_path="model", flavor=mlflow.pyfunc, training_set=training_set,
                 registered_model_name=MODEL_NAME, code_paths=CODE_PATHS,
                 extra_pip_requirements=[f"torch=={torch.__version__.split('+')[0]}"])
    # The comparator, registered only so its endpoint latency can be measured against DCNv2's.
    fe.log_model(model=SerpLgbmRanker(ranker), artifact_path="lgbm_model", flavor=mlflow.pyfunc,
                 training_set=training_set, registered_model_name=LGBM_NAME, code_paths=CODE_PATHS,
                 extra_pip_requirements=[f"lightgbm=={lightgbm.__version__}"])


def run_version(name):
    return max(int(v.version) for v in mlflow.MlflowClient().search_model_versions(f"name='{name}'")
               if v.run_id == run.info.run_id)


version, lgbm_version = run_version(MODEL_NAME), run_version(LGBM_NAME)
print(f"{MODEL_NAME} v{version}, {LGBM_NAME} v{lgbm_version} (run {run.info.run_id})")

# COMMAND ----------

# DBTITLE 1,Check: the registered models score raw rows like training
# Latency is measured against the endpoints (ml/scripts/bench_serp_l2.py), not here.
loaded = mlflow.pyfunc.load_model(f"runs:/{run.info.run_id}/model/data/feature_store/raw_model")  # loads onto the GPU
diff_loaded = float(np.abs(np.asarray(loaded.predict(keyless)) - expected).max())
assert diff_loaded < 1e-4, f"reloaded DCNv2 scores differ by up to {diff_loaded}"
loaded_lgbm = mlflow.pyfunc.load_model(f"runs:/{run.info.run_id}/lgbm_model/data/feature_store/raw_model")
diff_lgbm = float(np.abs(np.asarray(loaded_lgbm.predict(keyless)) - ranker.predict(model_matrix(keyless))).max())
assert diff_lgbm < 1e-9, f"reloaded LightGBM scores differ by up to {diff_lgbm}"

# COMMAND ----------

# DBTITLE 1,Summary
summary = {"model": MODEL_NAME, "version": version, "lgbm_model": LGBM_NAME, "lgbm_version": lgbm_version,
           "run_id": run.info.run_id, "gpu": torch.cuda.get_device_name(0),
           "searches": {"train": len(train_meta), "valid": len(valid_meta), "test": len(test_meta)},
           "best_epoch": model.best_epoch, "valid_ndcg10": round(model.best_valid_ndcg10, 4),
           "train_seconds": train_seconds, "lgbm_seconds": lgbm_seconds, "lgbm_best_iteration": ranker.best_iteration_,
           "ceiling": ceiling,
           "overall": {k: {m: round(v[m], 4) for m in ("n", "l1", "l2", "lift", "lift_lo", "lift_hi")}
                       for k, v in overall.items()},
           "slices": {k: [{m: (round(r[m], 4) if isinstance(r[m], float) else r[m])
                           for m in ("slice", "value", "n", "l1", "l2", "lift", "lift_lo", "lift_hi")} for r in rs]
                      for k, rs in report.items()},
           "raw_vs_packed_max_diff": diff_raw}
print(json.dumps(summary, indent=2, default=str))
dbutils.notebook.exit(json.dumps(summary, default=str))
