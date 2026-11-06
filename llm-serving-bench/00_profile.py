# Databricks notebook source
# DBTITLE 1,Profile your request shape
# MAGIC %md
# MAGIC # Profile your request shape
# MAGIC
# MAGIC Measures input and output tokens per request from your own data, so `02_benchmark` runs at your
# MAGIC workload's shape instead of the default 256 in / 128 out. Put the median (or P90) into
# MAGIC `load_test.input_tokens` and, for chat, `load_test.max_tokens`.
# MAGIC
# MAGIC | Source | Set | Measures |
# MAGIC |---|---|---|
# MAGIC | Sample file, embedding | `samples_path`: JSONL, one `{"text": ...}` per line | Input tokens, counted with the model's tokenizer; no endpoint calls |
# MAGIC | Sample file, chat | `samples_path`: JSONL, one `{"messages": [...]}` per line; `endpoint` | Input and output tokens reported by `endpoint`, replies at natural length (no `max_tokens`) |
# MAGIC | MLflow traces, chat | `experiment_id` | Token usage recorded in the traces (summed over an agent's LLM calls) |
# MAGIC
# MAGIC Use real requests: the full system prompt, retrieved context, chat history at several turn depths.
# MAGIC For `endpoint`, the model's pay-per-token endpoint from `00_discover` gives that model's reply lengths.
# MAGIC
# MAGIC **Compute:** Serverless CPU. The notebook exits with `{n, input_tokens, output_tokens}` as JSON.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "transformers>=4.40,<5" -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Measure
import json
import sys

import requests
from databricks.sdk import WorkspaceClient

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
sys.path.insert(0, "/Workspace" + "/".join(_nb_path.split("/")[:-1]) + "/lib")
from common import token_stats, trace_token_usage

dbutils.widgets.text("hf_name", "Alibaba-NLP/gte-large-en-v1.5")
dbutils.widgets.dropdown("task", "embedding", ["embedding", "chat"])
dbutils.widgets.text("samples_path", "")   # /Workspace/... or /Volumes/... JSONL
dbutils.widgets.text("endpoint", "")       # chat with samples_path: endpoint that generates replies
dbutils.widgets.text("experiment_id", "")  # chat: read token usage from this experiment's traces
TASK = dbutils.widgets.get("task")
SAMPLES_PATH, ENDPOINT = dbutils.widgets.get("samples_path"), dbutils.widgets.get("endpoint")
EXPERIMENT_ID = dbutils.widgets.get("experiment_id")

usages = []
if EXPERIMENT_ID:
    import mlflow

    traces = mlflow.search_traces(experiment_ids=[EXPERIMENT_ID], filter_string="status = 'OK'",
                                  return_type="list", max_results=500)
    usages = [u for u in (trace_token_usage(t.to_dict()) for t in traces) if u]
    print(f"{len(usages)} of {len(traces)} traces have token usage")
elif SAMPLES_PATH:
    with open(SAMPLES_PATH) as f:
        samples = [json.loads(line) for line in f if line.strip()]
    if TASK == "embedding":
        from transformers import AutoTokenizer

        tokenizer = AutoTokenizer.from_pretrained(dbutils.widgets.get("hf_name"))
        usages = [{"input_tokens": len(tokenizer.encode(s["text"], add_special_tokens=False))} for s in samples]
    else:
        if not ENDPOINT:
            raise ValueError("Set the endpoint widget: chat samples need an endpoint to generate replies")
        w = WorkspaceClient()
        token = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
        url = f"{w.config.host.rstrip('/')}/serving-endpoints/{ENDPOINT}/invocations"
        for i, s in enumerate(samples):
            resp = requests.post(url, headers={"Authorization": f"Bearer {token}"}, timeout=300,
                                 json={"messages": s["messages"], "temperature": 0.0})
            resp.raise_for_status()
            usage = resp.json()["usage"]
            usages.append({"input_tokens": usage["prompt_tokens"], "output_tokens": usage["completion_tokens"]})
            print(f"[{i + 1}/{len(samples)}] in={usages[-1]['input_tokens']} out={usages[-1]['output_tokens']}")
else:
    raise ValueError("Set samples_path, or experiment_id for chat traces")

if not usages:
    raise ValueError("No token counts measured; check the samples or traces")
result = {
    "n": len(usages),
    "input_tokens": token_stats([u["input_tokens"] for u in usages]),
    "output_tokens": token_stats([u["output_tokens"] for u in usages if "output_tokens" in u]),
}
print(json.dumps(result, indent=2))
dbutils.notebook.exit(json.dumps(result))
