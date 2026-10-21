# Databricks notebook source
# DBTITLE 1,Deploy selected serving endpoints
# MAGIC %md
# MAGIC # Deploy selected serving endpoints
# MAGIC
# MAGIC Creates the dedicated endpoints listed in the config (`configs/<model>.yml`) and smoke-tests every endpoint:
# MAGIC
# MAGIC * **PT** — created from the `system.ai` model at the configured capacity.
# MAGIC * **custom** — the HF model is logged from this notebook and registered with Express
# MAGIC   `env_pack`, so serving replays this exact environment (torch, CUDA, transformers).
# MAGIC * **ppt** — shared endpoint; only smoke-tested.
# MAGIC
# MAGIC Existing endpoints with the same name are reused, not modified.
# MAGIC
# MAGIC **Compute:** Serverless GPU (A10) with the AI environment in `model.ai_env` when any
# MAGIC custom endpoint is selected. Custom models must be logged from GPU compute; CPU-logged models
# MAGIC break at GPU serving time. PT/PPT-only configs can run on any compute.
# MAGIC
# MAGIC **Fails fast:** before creating anything, the model is loaded and run once on this GPU. If that
# MAGIC fails (usually a library-version mismatch, e.g. remote code written for transformers 4.x on an env
# MAGIC with 5.x), nothing is registered or created; run `00_compat_check` on the other AI env candidates.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "mlflow>=3.12" "databricks-sdk>=0.102.0" sentence-transformers pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import os
import sys
import time

import mlflow
import pandas as pd
from databricks.sdk import WorkspaceClient
from databricks.sdk.errors import NotFound

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
PROJECT_DIR = "/Workspace" + "/".join(_nb_path.split("/")[:-1])
sys.path.insert(0, PROJECT_DIR)
from common import (ModelCheckError, endpoint_label, incompatibility_message, library_versions, load_and_check,
                    load_config, parse_response, request_payload, wait_for_endpoint)

dbutils.widgets.text("config_path", "")  # e.g. {PROJECT_DIR}/configs/<model>.yml
if not dbutils.widgets.get("config_path"):
    raise ValueError(f"Set the config_path widget, e.g. {PROJECT_DIR}/configs/<model>.yml")
CFG = load_config(dbutils.widgets.get("config_path"))

mlflow.set_registry_uri("databricks-uc")
w = WorkspaceClient()

MODEL = CFG["model"]
TASK = MODEL["task"]
UC = CFG["unity_catalog"]
REGISTERED_MODEL_NAME = f"{UC['catalog']}.{UC['schema']}.{MODEL['registered_name']}"
ENDPOINTS = CFG["endpoints"]
CUSTOM = [ep for ep in ENDPOINTS if ep["method"] == "custom"]
TAGS = [{"key": "created_by", "value": "serving-bench"}]

print(f"Model: {MODEL['hf_name']} ({TASK})")
for ep in ENDPOINTS:
    print(f"  {ep['method']:<6} {ep['name']}  [{endpoint_label(ep)}]")

# COMMAND ----------

# DBTITLE 1,Check the model runs in this environment (custom endpoints only)
# Fail fast, before anything billable exists: load the HF model on this GPU and run one inference.
if CUSTOM:
    LOADED, info = load_and_check(MODEL, MODEL["ai_env"])
    print(f"Model check passed on AI env v{MODEL['ai_env']}: {info}")

# COMMAND ----------

# DBTITLE 1,Create PT endpoints
# PT first: it needs no model logging and takes longest to provision.
def endpoint_exists(name):
    try:
        w.serving_endpoints.get(name=name)
        return True
    except NotFound:
        return False


def create_endpoint(name, served_entity):
    if endpoint_exists(name):
        print(f"  {name}: exists — reusing as is")
        return
    w.api_client.do(
        "POST",
        "/api/2.0/serving-endpoints",
        body={"name": name, "config": {"served_entities": [served_entity]}, "tags": TAGS},
    )
    print(f"  {name}: created — {w.config.host}/ml/endpoints/{name}")


PT_CAPACITY_KEYS = ("min_provisioned_throughput", "max_provisioned_throughput", "provisioned_model_units")
for ep in ENDPOINTS:
    if ep["method"] == "pt":
        entity = {"name": "served", "entity_name": ep["entity_name"], "entity_version": str(ep["entity_version"])}
        entity |= {k: ep[k] for k in PT_CAPACITY_KEYS if k in ep}
        create_endpoint(ep["name"], entity)

# COMMAND ----------

# DBTITLE 1,Log and register the custom model (custom endpoints only)
if CUSTOM:
    import tempfile

    import torch
    import transformers
    from mlflow.models import infer_signature
    from mlflow.utils.env_pack import EnvPackConfig

    model_path = os.path.join(tempfile.mkdtemp(), "model")

    if TASK == "embedding":
        class EmbeddingModel(mlflow.pyfunc.PythonModel):
            def load_context(self, context):
                from sentence_transformers import SentenceTransformer

                self.model = SentenceTransformer(
                    context.artifacts["model_path"], device="cuda",
                    trust_remote_code=context.model_config["trust_remote_code"],
                )

            def predict(self, context, model_input, params=None):
                texts = model_input["text"].tolist()
                return self.model.encode(texts, normalize_embeddings=True).tolist()

        encoder, LOADED = LOADED, None
        encoder.save(model_path)  # keeps the pooling/normalize modules, unlike save_pretrained
        sample = pd.DataFrame({"text": ["serving-bench smoke test"]})
        log_kwargs = {
            "python_model": EmbeddingModel(),
            "input_example": sample,
            "signature": infer_signature(sample, encoder.encode(sample["text"].tolist()).tolist()),
            "model_config": {"trust_remote_code": MODEL["trust_remote_code"]},
        }
        print(f"Embedding dim: {encoder.get_sentence_embedding_dimension()}")
        del encoder
    else:
        from mlflow.pyfunc import ChatModel
        from mlflow.types.llm import ChatCompletionResponse

        class ChatAdapter(ChatModel):
            """Single-GPU HF generation behind the standard chat schema."""

            def load_context(self, context):
                import torch
                from transformers import AutoModelForCausalLM, AutoTokenizer

                path = context.artifacts["model_path"]
                self.tokenizer = AutoTokenizer.from_pretrained(path)
                self.model = AutoModelForCausalLM.from_pretrained(path, torch_dtype=torch.float16).to("cuda").eval()

            def predict(self, context, messages, params):
                import torch

                encoded = self.tokenizer.apply_chat_template(
                    [m.to_dict() for m in messages], add_generation_prompt=True,
                    return_tensors="pt", return_dict=True,
                ).to("cuda")
                prompt_tokens = encoded["input_ids"].shape[-1]
                temperature = params.temperature or 0.0
                generation = {"max_new_tokens": params.max_tokens or 128, "do_sample": temperature > 0,
                              "pad_token_id": self.tokenizer.pad_token_id or self.tokenizer.eos_token_id}
                if temperature > 0:
                    generation["temperature"] = temperature
                with torch.inference_mode():
                    output = self.model.generate(**encoded, **generation)[0][prompt_tokens:]
                return ChatCompletionResponse(
                    choices=[{"index": 0, "finish_reason": "stop",
                              "message": {"role": "assistant",
                                          "content": self.tokenizer.decode(output, skip_special_tokens=True)}}],
                    usage={"prompt_tokens": prompt_tokens, "completion_tokens": len(output),
                           "total_tokens": prompt_tokens + len(output)},
                )

        for part in LOADED:  # (tokenizer, model) from the check above
            part.save_pretrained(model_path)
        del LOADED
        log_kwargs = {"python_model": ChatAdapter()}

    with mlflow.start_run(run_name=f"serving-bench-{MODEL['registered_name']}"):
        model_info = mlflow.pyfunc.log_model(
            name="model",
            artifacts={"model_path": model_path},
            pip_requirements=["sentence-transformers>=3.0", f"torch=={torch.__version__.split('+')[0]}",
                              f"transformers=={transformers.__version__}"],
            **log_kwargs,
        )
    torch.cuda.empty_cache()

    # Sanity-check the logged artifact before spending endpoint provisioning time on it.
    try:
        loaded = mlflow.pyfunc.load_model(model_info.model_uri)
        if TASK == "embedding":
            print("Logged model output dim:", len(loaded.predict(pd.DataFrame({"text": ["check"]}))[0]))
        else:
            print("Logged model reply:", loaded.predict({"messages": [{"role": "user", "content": "Say hi"}],
                                                         "max_tokens": 16})["choices"][0]["message"]["content"])
        del loaded
    except Exception as error:
        raise ModelCheckError(incompatibility_message(MODEL, {"ai_env": MODEL["ai_env"]} | library_versions(), error)) from error

    model_version = mlflow.register_model(
        model_uri=model_info.model_uri,
        name=REGISTERED_MODEL_NAME,
        env_pack=EnvPackConfig(name="databricks_model_serving"),
    )
    print(f"Registered {model_version.name} v{model_version.version} with Express env_pack")

    for ep in CUSTOM:
        create_endpoint(ep["name"], {
            "name": "served",
            "entity_name": model_version.name,
            "entity_version": str(model_version.version),
            "workload_type": ep["workload_type"],
            "workload_size": "Small",
            "scale_to_zero_enabled": ep.get("scale_to_zero_enabled", False),
        })

# COMMAND ----------

# DBTITLE 1,Wait for every endpoint and smoke test
def smoke_test(ep, retries=5, delay=15):
    """Retries cover routing propagation right after READY."""
    payload = request_payload(ep["method"], TASK, "serving-bench smoke test", max_tokens=16)
    for attempt in range(1, retries + 1):
        try:
            start = time.time()
            body = w.api_client.do("POST", f"/serving-endpoints/{ep['name']}/invocations", body=payload)
            print(f"  {ep['name']}: OK {(time.time() - start) * 1000:.0f}ms {parse_response(TASK, body)}")
            return True
        except Exception as error:
            print(f"  {ep['name']}: attempt {attempt}/{retries} — {str(error)[:150]}")
            if attempt < retries:
                time.sleep(delay)
    return False


failed = [ep["name"] for ep in ENDPOINTS if not (wait_for_endpoint(w, ep["name"]) and smoke_test(ep))]
if failed:
    raise RuntimeError(f"Endpoints not ready or failing smoke test: {failed}")
print("\nAll endpoints ready. Run 02_benchmark next.")

# COMMAND ----------

# DBTITLE 1,Cleanup (uncomment to delete dedicated endpoints)
# Dedicated endpoints bill while running. Shared ppt endpoints are never deleted.
# for ep in ENDPOINTS:
#     if ep["method"] != "ppt":
#         w.serving_endpoints.delete(name=ep["name"])
#         print(f"  Deleted: {ep['name']}")
