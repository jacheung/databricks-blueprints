# Databricks notebook source
# DBTITLE 1,SERP query embedding model (log on GPU, Express env_pack)
# MAGIC %md
# MAGIC # SERP query embedding model (log on GPU, Express env_pack)
# MAGIC
# MAGIC Logs `models.query_embedding.hf_name` (gte-large-en-v1.5, the model behind `ai_search.embedding_endpoint`) as a
# MAGIC sentence-transformers pyfunc and registers it with Express `env_pack`, following
# MAGIC `production-llm-serving/serving-bench` `01_deploy`. `serp_query_embedding` then serves it on a dedicated GPU.
# MAGIC
# MAGIC **Raw vectors, not normalized.** The pay-per-token endpoint returns unnormalized GTE vectors (norm ~24); the index
# MAGIC stored product vectors the same way and ranks by L2 distance, so a unit-normalized query vector points the same
# MAGIC way but reorders results (4.5% hybrid overlap measured, vs 99.5% raw).
# MAGIC
# MAGIC Checks, before anything is served: the logged model's vectors match pay-per-token for sampled test-split queries,
# MAGIC in direction (cosine) and length.
# MAGIC
# MAGIC **Compute:** Serverless GPU (A10) with AI environment v5 (`models.query_embedding.ai_env`); GTE v1.5's remote
# MAGIC code fails on v6. Models logged on CPU break at GPU serving time.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "mlflow>=3.12" "databricks-sdk>=0.102.0" sentence-transformers pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import json
import math
import os
import sys
import tempfile

import mlflow
import pandas as pd
import torch
import transformers
from mlflow.models import infer_signature
from mlflow.utils.env_pack import EnvPackConfig
from sentence_transformers import SentenceTransformer

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
ROOT = "/Workspace" + "/".join(_nb_path.split("/")[:-3])  # this notebook is <ROOT>/ml/notebooks/<name>
sys.path[:0] = [f"{ROOT}/shared/src"]
from rts_shared.config import load_config, model_name, resolve_auto, table

from databricks.sdk import WorkspaceClient

dbutils.widgets.text("config_path", f"{ROOT}/shared/configs/dev.yml")  # jobs pass configs/<bundle target>.yml
w = WorkspaceClient()
CFG = resolve_auto(load_config(dbutils.widgets.get("config_path")), w.current_user.me().user_name)
QE = CFG["models"]["query_embedding"]
MODEL_NAME = model_name(CFG, "query_embedding")
assert os.environ.get("DATABRICKS_ACCELERATOR"), "run on Serverless GPU: models logged on CPU break at GPU serving"
MIN_COSINE, MAX_NORM_GAP = 0.999, 0.01

# COMMAND ----------

# DBTITLE 1,Load the model on this GPU
encoder = SentenceTransformer(QE["hf_name"], device="cuda", trust_remote_code=True)
model_path = os.path.join(tempfile.mkdtemp(), "model")
encoder.save(model_path)  # keeps the pooling modules, unlike save_pretrained
print("dim", encoder.get_sentence_embedding_dimension(), "torch", torch.__version__, "transformers",
      transformers.__version__)

# COMMAND ----------

# DBTITLE 1,Log and register (Express env_pack)
class EmbeddingModel(mlflow.pyfunc.PythonModel):
    """Raw (unnormalized) vectors, matching the pay-per-token endpoint the index was built with."""

    def load_context(self, context):
        from sentence_transformers import SentenceTransformer

        self.model = SentenceTransformer(context.artifacts["model_path"], device="cuda", trust_remote_code=True)

    def predict(self, context, model_input, params=None):
        return self.model.encode(model_input["text"].tolist(), normalize_embeddings=False).tolist()


sample = pd.DataFrame({"text": ["camera bag for sony"]})
experiment = CFG["mlflow"]["experiment"]
w.workspace.mkdirs(os.path.dirname(experiment))
mlflow.set_experiment(experiment)
mlflow.set_registry_uri("databricks-uc")
with mlflow.start_run(run_name="query_embedding_model"):
    info = mlflow.pyfunc.log_model(
        name="model", python_model=EmbeddingModel(), artifacts={"model_path": model_path}, input_example=sample,
        signature=infer_signature(sample, encoder.encode(sample["text"].tolist()).tolist()),
        pip_requirements=["sentence-transformers>=3.0", f"torch=={torch.__version__.split('+')[0]}",
                          f"transformers=={transformers.__version__}"])
del encoder
torch.cuda.empty_cache()

# COMMAND ----------

# DBTITLE 1,Check: the logged model matches pay-per-token in direction and length
queries = [r["query"] for r in spark.table(table(CFG, "qac_impressions")).where("split = 'test'")
           .select("query").distinct().orderBy("query").limit(64).collect()]
loaded = mlflow.pyfunc.load_model(info.model_uri)
ours = loaded.predict(pd.DataFrame({"text": queries}))
ppt = [d.embedding for i in range(0, len(queries), 16)
       for d in w.serving_endpoints.query(name=CFG["ai_search"]["embedding_endpoint"], input=queries[i:i + 16]).data]
del loaded


def norm(v):
    return math.sqrt(sum(x * x for x in v))


cosines = [sum(a * b for a, b in zip(x, y)) / (norm(x) * norm(y)) for x, y in zip(ours, ppt)]
norm_gaps = [abs(norm(x) / norm(y) - 1) for x, y in zip(ours, ppt)]
parity = {"queries": len(queries), "cosine_min": round(min(cosines), 6), "norm_gap_max": round(max(norm_gaps), 4),
          "norm_mean": round(sum(map(norm, ours)) / len(ours), 2)}
print(parity)
assert parity["cosine_min"] >= MIN_COSINE and parity["norm_gap_max"] <= MAX_NORM_GAP, \
    f"logged model doesn't reproduce pay-per-token vectors: {parity}"

version = mlflow.register_model(model_uri=info.model_uri, name=MODEL_NAME,
                                env_pack=EnvPackConfig(name="databricks_model_serving")).version
print(f"Registered {MODEL_NAME} v{version} with Express env_pack")

# COMMAND ----------

summary = {"model": MODEL_NAME, "version": version, "parity": parity}
print(json.dumps(summary, indent=2))
dbutils.notebook.exit(json.dumps(summary))
