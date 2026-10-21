# Databricks notebook source
# DBTITLE 1,GTE Deployment: Express vs Classic GPU Serving
# MAGIC %md
# MAGIC # GTE Deployment: Express vs Classic GPU Serving
# MAGIC
# MAGIC Exploring deployment paths for **Alibaba-NLP/gte-large-en-v1.5** on custom T4 and A10G GPUs.
# MAGIC
# MAGIC ## Background
# MAGIC GTE v1.5 uses custom RoPE CUDA kernels that crashed with `CUDA unspecified launch failure` on T4/A10G
# MAGIC via classic custom GPU serving. The classic serving container uses **PyTorch 1.13–2.0.1** with older CUDA,
# MAGIC which is likely incompatible with GTE's custom ops.
# MAGIC
# MAGIC ## Deployment Matrix
# MAGIC
# MAGIC | # | Logging Environment | Serving Method | GPU | Viable? | Why |
# MAGIC |---|---|---|---|---|---|
# MAGIC | 1 | Classic MLR + GPU cluster | Classic Custom GPU | T4 / A10G | \~\~Already failed\~\~ | Old serving container (torch 1.13–2.0.1), CUDA kernel crash |
# MAGIC | 2 | Classic MLR + GPU cluster | Express Deployment | T4 / A10G | Not possible | Express **requires** logging from Serverless GPU |
# MAGIC | 3 | **Serverless GPU (AI v5/v6)** | **Express Deployment** | **T4 / A10G** | **Best bet** | Serving env = notebook env (modern torch + CUDA), no container build |
# MAGIC | 4 | Serverless GPU (AI v5/v6) | Classic Custom GPU | T4 / A10G | Risky | Model logged with modern deps but served on old container |
# MAGIC
# MAGIC ## Why Express Deployments Should Fix This
# MAGIC * Express packages the **exact** notebook environment (torch, CUDA, transformers) and replays it in serving
# MAGIC * AI env v5/v6 ships modern PyTorch (2.x) + CUDA 12.x — GTE's custom RoPE kernels should compile/run fine
# MAGIC * No container image build step → faster deployment
# MAGIC * `env_pack="databricks_model_serving"` at registration is the only API difference
# MAGIC
# MAGIC ## Requirements
# MAGIC * **Compute:** Serverless GPU (A10 or H100), **AI env v5** (NOT v6 — v6 ships transformers 5.x which breaks GTE's custom code)
# MAGIC * **SDK:** `mlflow >= 3.12`, `databricks-sdk >= 0.102.0`
# MAGIC * Must log + register from the GPU runtime (CPU logging → GPU serving = broken deps)
# MAGIC
# MAGIC ## Key Finding: transformers Version Matters
# MAGIC * AI env **v5** → transformers 4.57.1 → ✅ GTE works
# MAGIC * AI env **v6** → transformers 5.8.1 → ❌ GTE's custom `Alibaba-NLP/new-impl` RoPE code crashes (index out of bounds)
# MAGIC * Using AI v5 natively means no pip overrides → cleanest Express packaging

# COMMAND ----------

# DBTITLE 1,Step 1: Environment Check & Dependencies
# ── Fail-fast: this notebook MUST run on Serverless GPU ──────────────
import os, sys

accel = os.environ.get("DATABRICKS_ACCELERATOR")
if not accel:
    raise RuntimeError(
        "⚠️  This notebook MUST run on Serverless GPU (A10 or H100).\n"
        "Go to Connect → Serverless GPU, then set Base environment to AI v5 or v6.\n"
        "CPU-logged models will fail at GPU serving time."
    )
print(f"✅ Serverless GPU detected: accelerator = {accel}")

# Show torch + CUDA versions (these will be mirrored in the Express serving container)
import torch
print(f"   PyTorch: {torch.__version__}")
print(f"   CUDA:    {torch.version.cuda}")
print(f"   GPU:     {torch.cuda.get_device_name(0)}")

# COMMAND ----------

# DBTITLE 1,Step 2: Install Express Deployment Dependencies
# AI v5 ships transformers 4.57.1 natively — no need to override it.
# Only install mlflow/SDK upgrades and sentence-transformers.
%pip install "mlflow>=3.12" "databricks-sdk>=0.102.0" sentence-transformers -q

# COMMAND ----------

# DBTITLE 1,Step 2b: Restart Python after pip install
dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Step 3: Load Shared Config
import os, time, yaml
import pandas as pd
import mlflow
from mlflow import MlflowClient
from databricks.sdk import WorkspaceClient

mlflow.set_registry_uri("databricks-uc")
w = WorkspaceClient()

# ── Load shared config ─────────────────────────────────────────────
_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
_project_dir = "/Workspace" + "/".join(_nb_path.split("/")[:-1])
_config_path = f"{_project_dir}/config.yml"

with open(_config_path) as f:
    CFG = yaml.safe_load(f)
print(f"Loaded config from {_config_path}")

# ── Derived constants ──────────────────────────────────────────────
CATALOG = CFG["unity_catalog"]["catalog"]
SCHEMA  = CFG["unity_catalog"]["schema"]

GTE_HF_MODEL        = CFG["models"]["gte"]["hf_name"]
REGISTERED_MODEL_NAME = f"{CATALOG}.{SCHEMA}.{CFG['models']['gte']['registered_name']}"

PT_SYSTEM_MODEL     = CFG["models"]["gte"]["pt_system_model"]

EP = CFG["endpoints"]
T4_ENDPOINT   = EP["express_t4"]["name"]
A10G_ENDPOINT = EP["express_a10g"]["name"]
PT_ENDPOINT   = EP["pt"]["name"]

print(f"\nModel:      {GTE_HF_MODEL}")
print(f"UC name:    {REGISTERED_MODEL_NAME}")
print(f"Endpoints:  {T4_ENDPOINT} (T4), {A10G_ENDPOINT} (A10G), {PT_ENDPOINT} (PT)")
print(f"AI env:     {CFG['required_ai_env']} (required for transformers 4.x)")

# ── Auth tokens for REST API calls ─────────────────────────────────
TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
WORKSPACE_URL = w.config.host.rstrip("/")

# COMMAND ----------

# DBTITLE 1,Step 4: Load GTE & Verify on GPU
import torch
from sentence_transformers import SentenceTransformer

print(f"Loading {GTE_HF_MODEL} (the model with custom RoPE CUDA kernels)...")
gte_model = SentenceTransformer(GTE_HF_MODEL, trust_remote_code=True)
print(f"  Parameters: {sum(p.numel() for p in gte_model.parameters()):,}")
print(f"  Device:     {gte_model.device}")

# Verify the custom CUDA kernels work on this GPU
test_texts = ["Express deployment test for GTE with custom RoPE CUDA kernels"]
try:
    embeddings = gte_model.encode(test_texts, normalize_embeddings=True)
    print(f"  Embedding dim: {embeddings.shape[1]}")
    print(f"  ✅ GTE inference works on {torch.cuda.get_device_name(0)}!")
    print(f"     (custom RoPE CUDA kernels are functioning correctly)")
except Exception as e:
    print(f"  ❌ GTE inference FAILED: {e}")
    print(f"     The custom CUDA kernels still don't work on this GPU.")

# COMMAND ----------

# DBTITLE 1,Log & Register GTE with Express Packaging
import tempfile
import numpy as np
from mlflow.models import infer_signature
from mlflow.utils.env_pack import EnvPackConfig
# os, time, pd, torch, mlflow, w — already in scope from Steps 3–4.

# ── Save HF model files for artifact packaging ────────────────────
from transformers import AutoModel, AutoTokenizer
tmpdir = tempfile.mkdtemp()
model_save_path = os.path.join(tmpdir, "gte-large-en-v1.5")

print(f"Saving {GTE_HF_MODEL} for artifact packaging...")
_model = AutoModel.from_pretrained(GTE_HF_MODEL, trust_remote_code=True)
_tok = AutoTokenizer.from_pretrained(GTE_HF_MODEL, trust_remote_code=True)
_model.save_pretrained(model_save_path)
_tok.save_pretrained(model_save_path)
print(f"  Saved to {model_save_path}")

# ── Pyfunc wrapper using SentenceTransformer ──────────────────────
class GTEEmbeddingModel(mlflow.pyfunc.PythonModel):
    def load_context(self, context):
        from sentence_transformers import SentenceTransformer
        import torch
        self.device = "cuda" if torch.cuda.is_available() else "cpu"
        self.model = SentenceTransformer(
            context.artifacts["model_path"],
            device=self.device,
            trust_remote_code=True,
        )
        print(f"GTE loaded on {self.device} ({torch.cuda.get_device_name(0) if self.device == 'cuda' else 'CPU'})")

    def predict(self, context, model_input, params=None):
        if isinstance(model_input, pd.DataFrame):
            texts = model_input.iloc[:, 0].tolist()
        elif isinstance(model_input, list):
            texts = model_input
        else:
            texts = [str(model_input)]
        embeddings = self.model.encode(texts, normalize_embeddings=True)
        return embeddings.tolist()

# ── Build signature from a live prediction ────────────────────────
sample_input = pd.DataFrame({"text": ["sample text for embedding"]})
sample_output = gte_model.encode(["sample text for embedding"], normalize_embeddings=True).tolist()
signature = infer_signature(sample_input, [sample_output])
print(f"Signature: {signature}")

# ── Log model to MLflow ───────────────────────────────────────────
import transformers
print(f"\nLogging GTE model (transformers={transformers.__version__}, torch={torch.__version__})...")
with mlflow.start_run(run_name="gte-large-express-v1") as run:
    model_info = mlflow.pyfunc.log_model(
        artifact_path="model",
        python_model=GTEEmbeddingModel(),
        artifacts={"model_path": model_save_path},
        pip_requirements=[
            # On AI v5, transformers 4.57.1 is native — no need to hardpin.
            # Express env_pack captures the exact env regardless.
            "sentence-transformers>=3.0",
            "torch>=2.0",
            "transformers>=4.40,<5",  # Guard against 5.x in case env changes
        ],
        registered_model_name=None,
        input_example=sample_input,
        signature=signature,
    )
    print(f"  Run ID:    {run.info.run_id}")
    print(f"  Model URI: {model_info.model_uri}")

del _model; torch.cuda.empty_cache()

# ── Register with Express packaging (env_pack) ───────────────────
print(f"\n🚀 Registering with env_pack (Express Deployment)...")
print(f"   This packages the EXACT notebook environment for serving.")
print(f"   transformers={transformers.__version__}, torch={torch.__version__}, CUDA={torch.version.cuda}")

model_version = mlflow.register_model(
    model_uri=model_info.model_uri,
    name=REGISTERED_MODEL_NAME,
    env_pack=EnvPackConfig(name="databricks_model_serving"),
)
print(f"\n  ✅ Registered: {model_version.name} v{model_version.version}")
print(f"     Express packaging complete — serving will use transformers {transformers.__version__}")

# COMMAND ----------

# DBTITLE 1,Deploy Express Endpoints (T4 + A10G)
from databricks.sdk.service.serving import (
    EndpointCoreConfigInput,
    ServedEntityInput,
    ServingModelWorkloadType,
)

endpoints_config = [
    (T4_ENDPOINT,  ServingModelWorkloadType.GPU_SMALL,  "T4 (16GB)"),
    (A10G_ENDPOINT, ServingModelWorkloadType.GPU_MEDIUM, "A10G (24GB)"),
]

for ep_name, workload, gpu_desc in endpoints_config:
    print(f"\n{'='*60}")
    print(f"Creating Express endpoint: {ep_name} ({gpu_desc})")
    print(f"  Model:    {model_version.name} v{model_version.version}")
    print(f"  Workload: {workload}")
    print(f"{'='*60}")

    try:
        existing = w.serving_endpoints.get(name=ep_name)
        ready = existing.state.ready
        print(f"  Endpoint already exists (ready={ready}).")
        # If it's from a previous failed run, delete and recreate
        from databricks.sdk.service.serving import EndpointStateReady
        if ready == EndpointStateReady.READY:
            print(f"  Already READY — skipping.")
            continue
        print(f"  Not ready — deleting and recreating...")
        w.serving_endpoints.delete(name=ep_name)
        import time; time.sleep(5)
    except Exception:
        pass  # Doesn't exist, create it

    try:
        w.serving_endpoints.create(
            name=ep_name,
            config=EndpointCoreConfigInput(
                name=ep_name,
                served_entities=[
                    ServedEntityInput(
                        name=ep_name.replace("-", "_"),
                        entity_name=model_version.name,
                        entity_version=model_version.version,
                        workload_type=workload,
                        workload_size="Small",
                        scale_to_zero_enabled=False,
                    )
                ],
            ),
        )
        print(f"  ✅ Created! Provisioning...")
        print(f"  Monitor: {w.config.host}/ml/endpoints/{ep_name}")
        print(f"  (Express = no container build, faster deploy)")
    except Exception as e:
        print(f"  ❌ Error: {e}")

# COMMAND ----------

# DBTITLE 1,Discover PT Embedding Models in system.ai
# List embedding models available in system.ai for provisioned throughput
embedding_keywords = ["bge", "gte", "embed", "e5", "qwen3_embedding"]

try:
    all_models = list(w.registered_models.list(catalog_name="system", schema_name="ai"))
    embedding_models = [
        m for m in all_models
        if any(kw in m.name.lower() for kw in embedding_keywords)
    ]

    print(f"Found {len(embedding_models)} embedding model(s) in system.ai:\n")
    for m in embedding_models:
        marker = "  <-- SELECTED" if f"system.ai.{m.name}" == PT_SYSTEM_MODEL else ""
        comment = getattr(m, "comment", "") or ""
        print(f"  system.ai.{m.name}{marker}")
        if comment:
            print(f"    {comment[:120]}")
except Exception as e:
    print(f"Could not list system.ai models: {e}")
    print("Proceeding with pre-selected model: system.ai.bge_large_en")

# COMMAND ----------

# DBTITLE 1,Deploy Provisioned Throughput Endpoint
# PT endpoint uses system.ai model (BGE) — no custom logging needed.
# Provisioned via tokens-per-second (legacy PT model, not model units).
import requests as _requests
from databricks.sdk.service.serving import EndpointStateReady

pt_cfg = EP["pt"]
_headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

print(f"Creating PT endpoint '{pt_cfg['name']}'...")
print(f"  Model:      {PT_SYSTEM_MODEL}")
print(f"  Throughput: {pt_cfg['min_throughput']}–{pt_cfg['max_throughput']} tokens/sec\n")

# Check if it already exists
try:
    existing = w.serving_endpoints.get(name=pt_cfg["name"])
    ready = existing.state.ready
    print(f"  Already exists (ready={ready}).")
    if ready == EndpointStateReady.READY:
        print(f"  Already READY — skipping.")
    else:
        print(f"  Not ready — deleting and recreating...")
        w.serving_endpoints.delete(name=pt_cfg["name"])
        time.sleep(5)
        raise Exception("recreate")
except Exception:
    # Create via REST API (SDK doesn't support min/max_provisioned_throughput directly)
    payload = {
        "name": pt_cfg["name"],
        "config": {
            "served_entities": [{
                "entity_name": PT_SYSTEM_MODEL,
                "entity_version": "1",
                "min_provisioned_throughput": pt_cfg["min_throughput"],
                "max_provisioned_throughput": pt_cfg["max_throughput"],
            }]
        }
    }
    resp = _requests.post(
        f"{WORKSPACE_URL}/api/2.0/serving-endpoints",
        headers=_headers, json=payload
    )
    if resp.status_code == 200:
        print(f"  ✅ Created! Provisioning...")
        print(f"  Monitor: {WORKSPACE_URL}/ml/endpoints/{pt_cfg['name']}")
    elif "already exists" in resp.text.lower():
        print(f"  Already exists (via API). Skipping.")
    else:
        print(f"  ❌ Error {resp.status_code}: {resp.text[:300]}")

# COMMAND ----------

# DBTITLE 1,Wait for All Endpoints & Smoke Test
import requests
from databricks.sdk.service.serving import EndpointStateReady, EndpointStateConfigUpdate

def wait_for_endpoint(name, timeout_minutes=60, poll_interval=30):
    """Poll until READY, FAILED, or timeout.

    BUG FIX: Previous code used '"READY" in str(state.ready)' which
    matched EndpointStateReady.NOT_READY (string contains 'READY').
    Now uses proper enum comparison.
    """
    start = time.time()
    while time.time() - start < timeout_minutes * 60:
        ep = w.serving_endpoints.get(name=name)
        state = ep.state
        # Enum comparison — NOT string containment
        if state.ready == EndpointStateReady.READY:
            print(f"  {name}: ✅ READY")
            return True
        if state.config_update == EndpointStateConfigUpdate.UPDATE_FAILED:
            print(f"  {name}: ❌ FAILED — {state}")
            return False
        elapsed = int(time.time() - start)
        print(f"  {name}: {state.ready.value} / {state.config_update.value if state.config_update else '-'} ({elapsed}s)", end="\r")
        time.sleep(poll_interval)
    print(f"  {name}: ⏰ TIMEOUT after {timeout_minutes}min")
    return False

def test_endpoint(name, ep_type="custom", retries=5, delay=15):
    """Invoke the endpoint with retries for post-READY routing propagation."""
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}
    # PT/PPT use OpenAI embedding format; custom/express use MLflow pyfunc format
    if ep_type in ("pt", "ppt"):
        payload = {"input": ["Express deployment smoke test for GTE v1.5"]}
    else:
        payload = {"dataframe_records": [{"text": "Express deployment smoke test for GTE v1.5 on custom GPU"}]}
    url = f"{WORKSPACE_URL}/serving-endpoints/{name}/invocations"
    for attempt in range(1, retries + 1):
        start = time.time()
        resp = requests.post(url, headers=headers, json=payload)
        latency = (time.time() - start) * 1000
        if resp.status_code == 200:
            data = resp.json()
            # PT/PPT return OpenAI format: {"data": [{"embedding": [...]}]}
            # Custom/Express return pyfunc format: {"predictions": [...]}
            if "data" in data and data["data"]:
                emb = data["data"][0].get("embedding", [])
            else:
                emb = data.get("predictions", [[]])[0]
            dim = len(emb) if isinstance(emb, list) else "?"
            print(f"  {name}: ✅ {latency:.0f}ms, dim={dim}")
            return True
        print(f"  {name}: attempt {attempt}/{retries} — {resp.status_code} ({resp.text[:120]})")
        if attempt < retries:
            time.sleep(delay)
    print(f"  {name}: ❌ all {retries} attempts failed")
    return False

# Build full endpoint list from config: Express GPU + PT
all_deploy_endpoints = [
    (EP["express_t4"]["name"],   EP["express_t4"].get("gpu", "T4"),   EP["express_t4"]["type"]),
    (EP["express_a10g"]["name"], EP["express_a10g"].get("gpu", "A10G"), EP["express_a10g"]["type"]),
    (EP["pt"]["name"],           EP["pt"]["label"],                    EP["pt"]["type"]),
]

print("Waiting for all endpoints to reach READY state...\n")
for ep_name, ep_desc, ep_type in all_deploy_endpoints:
    print(f"\n{ep_name} ({ep_desc}):")
    if wait_for_endpoint(ep_name):
        test_endpoint(ep_name, ep_type=ep_type)

# COMMAND ----------

# DBTITLE 1,Cleanup (uncomment to delete endpoints)
# =====================================================================
#  CLEANUP: Uncomment the block below and run to tear down all
#  test endpoints. Per workspace policy, GPU endpoints without a
#  RemoveAfter tag are deleted after 3 days automatically.
# =====================================================================

# for key, ep_cfg in EP.items():
#     try:
#         w.serving_endpoints.delete(name=ep_cfg["name"])
#         print(f"  Deleted: {ep_cfg['name']}")
#     except Exception as e:
#         print(f"  Skip {ep_cfg['name']}: {e}")
#
# # Optionally remove the registered model version
# # from mlflow import MlflowClient
# # MlflowClient().delete_registered_model(name=REGISTERED_MODEL_NAME)
#
# print("\nCleanup complete.")

print("Cleanup cell ready. Uncomment the code above and run to delete all endpoints.")
print(f"  Endpoints:        {[ep_cfg['name'] for ep_cfg in EP.values()]}")
print(f"  Registered model: {REGISTERED_MODEL_NAME}")