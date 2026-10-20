# Databricks notebook source
# MAGIC %pip install openai

# COMMAND ----------

# MAGIC %md
# MAGIC # Workload Profiler — Pay-Per-Token
# MAGIC
# MAGIC Profiles **per-request** token and latency characteristics against a PPT endpoint.
# MAGIC Use the output as inputs to the ITPM/OTPM calculation and the Databricks GenAI Pricing Calculator.
# MAGIC
# MAGIC | Metric | Description | Used for |
# MAGIC |---|---|---|
# MAGIC | **Input tokens** | Prompt tokens per request | `ITPM = input_tokens x QPM` |
# MAGIC | **Output tokens** | Completion tokens per request | `OTPM = output_tokens x QPM` |
# MAGIC | **TTFT** | Time to first token (ms) | Latency baseline |
# MAGIC | **TPOT** | Time per output token (ms) | Latency baseline |

# COMMAND ----------

# MAGIC %md
# MAGIC ## How to use
# MAGIC
# MAGIC 1. Fill in the **CUSTOMER CONFIGURATION** cell below — set `PPT_MODEL`, replace `SYSTEM_PROMPT`
# MAGIC    with your real system prompt, and populate the sample lists that match your workload.
# MAGIC 2. Leave unused sample lists as `[]` — those workload cells are skipped automatically.
# MAGIC 3. **Run All** cells top to bottom.
# MAGIC 4. Read the summary table and PPT capacity ceiling at the bottom.
# MAGIC
# MAGIC | Sample list | Populate if you have... |
# MAGIC |---|---|
# MAGIC | `RAG_SAMPLES` | A vector DB retrieval step that assembles context into the prompt |
# MAGIC | `CHAT_SAMPLES` | A conversational assistant with session history |
# MAGIC | `TRACE_SAMPLES` | An agent that makes multiple LLM calls per request (uses MLflow traces) |

# COMMAND ----------

# ╔══════════════════════════════════════════════════════════════════════════╗
# ║                     CUSTOMER CONFIGURATION                              ║
# ║  Edit this cell. Do not modify any cells below the separator.           ║
# ╚══════════════════════════════════════════════════════════════════════════╝

# ── Model ─────────────────────────────────────────────────────────────────
# Change to the pay-per-token model you want to profile.
PPT_MODEL = "databricks-gpt-oss-20b"

# PPT rate limits — update when Databricks publishes new limits.
# https://docs.databricks.com/aws/en/machine-learning/foundation-model-apis/limits
PPT_LIMITS = {
    # GPT OSS
    "databricks-gpt-oss-120b":                 {"itpm": 200_000, "otpm": 10_000, "qph": None},
    "databricks-gpt-oss-20b":                  {"itpm": 200_000, "otpm": 10_000, "qph": None},
    # Meta Llama
    "databricks-meta-llama-4-maverick":        {"itpm": 200_000, "otpm": 10_000, "qph": 2_400},
    "databricks-meta-llama-3-3-70b-instruct":  {"itpm": 200_000, "otpm": 10_000, "qph": 2_400},
    "databricks-meta-llama-3-1-8b-instruct":   {"itpm": 200_000, "otpm": 10_000, "qph": 7_200},
    "databricks-meta-llama-3-1-405b-instruct": {"itpm": 5_000,   "otpm": 500,    "qph": 1_200},
    # Google
    "databricks-gemma-3-12b":                  {"itpm": 200_000, "otpm": 10_000, "qph": 7_200},
    "databricks-gemini-2-5-pro":               {"itpm": 200_000, "otpm": 20_000, "qph": 360_000},
    "databricks-gemini-2-5-flash":             {"itpm": 200_000, "otpm": 20_000, "qph": 360_000},
    # Anthropic Claude
    "databricks-claude-sonnet-4":              {"itpm": 200_000, "otpm": 20_000, "qph": 360_000},
    "databricks-claude-opus-4-7":              {"itpm": 200_000, "otpm": 20_000, "qph": 360_000},
    "databricks-claude-haiku-4-5":             {"itpm": 200_000, "otpm": 20_000, "qph": 360_000},
}

# ── System prompt ──────────────────────────────────────────────────────────
# Profile with your real system prompt — system tokens are added to every
# request, so a placeholder will structurally understate ITPM.
SYSTEM_PROMPT = """\
You are Aria, an AI assistant for a data and AI platform. You help data engineers, \
data scientists, and analysts with technical questions, troubleshooting, and best practices.

Responsibilities:
- Answer questions about clusters, notebooks, jobs, SQL warehouses, and ML workflows
- Troubleshoot errors by asking for error messages, logs, and runtime versions before suggesting fixes
- Explain pricing, product tiers, and capacity planning concepts clearly
- Escalate data loss, security incidents, and billing disputes to a human agent immediately

Guidelines:
- Always ask for the workspace tier before recommending features, as availability varies
- Default to Python for code examples unless the user specifies another language
- Keep responses concise and use numbered steps for multi-step procedures
- Do not speculate about roadmap or make commitments about future features

Tone: Match the user's technical depth. Be precise with technical users; avoid jargon with \
non-technical ones. Always be patient and direct.\
"""

# ── RAG_SAMPLES ────────────────────────────────────────────────────────────
# Each entry: a fully assembled messages list (system + retrieved context + question).
# Include real retrieved chunks — bare questions will significantly understate input tokens.
# Leave RAG_SAMPLES = [] to skip this workload.
_rag_context = """\
[Document 1]
Provisioned Throughput (PT) provides dedicated inference capacity for Foundation Model APIs. \
Unlike pay-per-token pricing, PT reserves model units — fixed compute allocations billed \
hourly regardless of usage. Each model unit translates to a guaranteed tokens-per-second \
throughput floor for a specific model. PT endpoints support burst scaling: when traffic \
temporarily exceeds provisioned capacity, Databricks can step up to the next model unit \
tier if regional capacity is available.

[Document 2]
Model units are sized based on your expected query rate and the average input/output token \
distribution of your requests. The Databricks GenAI Pricing Calculator accepts average input \
tokens per request, average output tokens per request, and queries per minute to recommend \
the appropriate number of model units. Output tokens are more compute-intensive than input \
tokens due to the autoregressive nature of decoding, so workloads with long outputs require \
more model units per QPM than workloads with short outputs.

[Document 3]
For production deployments, a common pattern is to pair a PT endpoint for baseline traffic \
with a pay-per-token fallback for overflow. The PT endpoint handles your provisioned QPM \
floor with guaranteed throughput, while the PPT model absorbs bursts that exceed PT capacity. \
This avoids the need to overprovision PT for peak traffic while ensuring requests are served \
during spikes.\
"""

RAG_SAMPLES = [
    [
        {"role": "system", "content": f"{SYSTEM_PROMPT}\nAnswer using only the provided context."},
        {"role": "user",   "content": f"Context:\n{_rag_context}\n\nQuestion: What is the difference between Provisioned Throughput and Pay-Per-Token, and when should I use each?"},
    ],
    # Add more entries — vary retrieved chunks and questions to capture realistic input lengths.
]

# ── CHAT_SAMPLES ───────────────────────────────────────────────────────────
# Each entry: (messages_list, label). Sample at multiple turn depths —
# input tokens grow with session history, so early-turn-only sampling
# understates average ITPM. Leave CHAT_SAMPLES = [] to skip.
_turn_1 = [
    {"role": "system",    "content": SYSTEM_PROMPT},
    {"role": "user",      "content": "My Databricks job keeps failing with an out-of-memory error. What should I check first?"},
]

_turn_3 = [
    {"role": "system",    "content": SYSTEM_PROMPT},
    {"role": "user",      "content": "My Databricks job keeps failing with an out-of-memory error. What should I check first?"},
    {"role": "assistant", "content": "Start by checking the Spark UI for the stage that failed — look at peak memory per executor and compare it to your cluster's executor memory. Also check for collect() or toPandas() calls that pull the full dataset to the driver. What's your cluster configuration and runtime version?"},
    {"role": "user",      "content": "It's a Standard_DS3_v2 with 4 workers, runtime 13.3 LTS. The error says 'GC overhead limit exceeded'."},
    {"role": "assistant", "content": "GC overhead on 13.3 LTS with DS3_v2 (14 GB per node) usually means a shuffle is materializing more data than fits in executor memory. Try increasing spark.sql.shuffle.partitions (default 200 is often too low) and enabling spark.sql.adaptive.enabled=true. Can you share the failing stage's input size from the Spark UI?"},
    {"role": "user",      "content": "The failing stage shows 48 GB input with 200 partitions. AQE is already on."},
]

_turn_6 = _turn_3 + [
    {"role": "assistant", "content": "48 GB across 200 partitions is 240 MB per partition — too large for DS3_v2 executors. Increase shuffle partitions to at least 1,000 (target 128-256 MB per partition). Also check for data skew: if one key dominates, salting the join key will help. What does the task duration distribution look like?"},
    {"role": "user",      "content": "Most tasks finish in 2-3 seconds but 3 tasks take over 10 minutes."},
    {"role": "assistant", "content": "Classic skew. Use the Databricks skew hint: JOIN /*+ SKEW('orders', 'customer_id') */. If that's insufficient, salt the key manually. Test the hint first since it requires no schema changes."},
    {"role": "user",      "content": "Yes, it's the customer_id column — a handful of large enterprise accounts drive most of the data."},
    {"role": "assistant", "content": "Known skew key is the easiest case. Rewrite the join with the skew hint and Databricks handles the split automatically. If still insufficient, append CONCAT(customer_id, '-', FLOOR(RAND() * 10)) on the large table and broadcast replicated keys from the small table."},
    {"role": "user",      "content": "Got it. Should I also increase the number of workers for this job?"},
]

CHAT_SAMPLES = [
    (_turn_1, "turn-1"),
    (_turn_3, "turn-3"),
    (_turn_6, "turn-6"),
]

# ── TRACE_SAMPLES ──────────────────────────────────────────────────────────
# MLflow trace dicts for agentic / tool-calling workloads.
# Retrieve real traces with:
#
#   import mlflow
#   mlflow.set_experiment("/your/experiment/path")
#   raw = mlflow.search_traces(filter_string="status = 'OK'", return_type="list", max_results=50)
#   TRACE_SAMPLES = [t.to_dict() for t in raw]
#
# The sample below mirrors the real MLflow trace JSON structure (.to_dict() output)
# so you can verify extraction is working before swapping in real data.
# Leave TRACE_SAMPLES = [] to skip this workload.
import json as _json

TRACE_SAMPLES = [
    {
        "info": {
            "request_id": "trace-abc123",
            "execution_time_ms": 3820,
            "status": "OK",
            "token_usage": {
                "input_tokens": 2340,
                "output_tokens": 187,
                "total_tokens": 2527,
            },
        },
        "data": {
            "spans": [
                {
                    "name": "agent",
                    "attributes": {"mlflow.spanType": "\"AGENT\""},
                },
                {
                    "name": "llm_call_1",
                    "attributes": {
                        "mlflow.spanType": "\"CHAT_MODEL\"",
                        "mlflow.chat.tokenUsage": _json.dumps(
                            {"input_tokens": 512, "output_tokens": 42, "total_tokens": 554}
                        ),
                    },
                },
                {
                    "name": "vector_search",
                    "attributes": {"mlflow.spanType": "\"TOOL\""},
                },
                {
                    "name": "llm_call_2",
                    "attributes": {
                        "mlflow.spanType": "\"CHAT_MODEL\"",
                        "mlflow.chat.tokenUsage": _json.dumps(
                            {"input_tokens": 1828, "output_tokens": 145, "total_tokens": 1973}
                        ),
                    },
                },
            ]
        },
    }
]

# COMMAND ----------

# MAGIC %md
# MAGIC ---
# MAGIC ## DO NOT EDIT BELOW
# MAGIC
# MAGIC All cells below this line are read-only profiling logic.
# MAGIC Edit only the **CUSTOMER CONFIGURATION** cell above.
# MAGIC
# MAGIC ---

# COMMAND ----------

import os

os.environ["DATABRICKS_HOST"]  = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiUrl().get()
os.environ["DATABRICKS_TOKEN"] = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()

# COMMAND ----------

from openai import OpenAI

client = OpenAI(
    base_url=f"{os.environ['DATABRICKS_HOST']}/serving-endpoints",
    api_key=os.environ["DATABRICKS_TOKEN"],
)

# COMMAND ----------

import json, statistics, time
from typing import Optional


def profile_request(messages: list[dict]) -> dict:
    """
    Two calls per query:
    1. Non-streaming: input/output token counts (usage is in the response object)
    2. Streaming: TTFT and TPOT (Databricks API does not support stream_options)

    TTFT = time from request start to first token received
    TPOT = (total_time - TTFT) / (output_tokens - 1)
    """
    response = client.chat.completions.create(
        model=PPT_MODEL,
        messages=messages,
    )
    input_tokens  = response.usage.prompt_tokens
    output_tokens = response.usage.completion_tokens

    ttft_ms = None
    t_start = time.perf_counter()

    stream = client.chat.completions.create(
        model=PPT_MODEL,
        messages=messages,
        stream=True,
    )
    for chunk in stream:
        if ttft_ms is None and chunk.choices and chunk.choices[0].delta.content:
            ttft_ms = (time.perf_counter() - t_start) * 1000

    total_ms = (time.perf_counter() - t_start) * 1000
    tpot_ms  = (total_ms - ttft_ms) / (output_tokens - 1) if output_tokens > 1 else 0.0

    return {
        "input_tokens":  input_tokens,
        "output_tokens": output_tokens,
        "ttft_ms":       round(ttft_ms, 2),
        "tpot_ms":       round(tpot_ms, 2),
        "total_ms":      round(total_ms, 2),
    }


def extract_token_usage(trace: dict) -> Optional[dict]:
    """
    Returns {input_tokens, output_tokens} for a trace dict.
    Uses trace-level token_usage (MLflow aggregates across all CHAT_MODEL spans)
    when available, then falls back to summing CHAT_MODEL spans individually.
    """
    usage = trace.get("info", {}).get("token_usage")
    if usage:
        return {"input_tokens": usage["input_tokens"], "output_tokens": usage["output_tokens"]}

    total_input, total_output = 0, 0
    for span in trace.get("data", {}).get("spans", []):
        attrs = span.get("attributes", {})
        if json.loads(attrs.get("mlflow.spanType", '"UNKNOWN"')) != "CHAT_MODEL":
            continue
        raw = attrs.get("mlflow.chat.tokenUsage") or attrs.get("llm.token_usage.input_tokens")
        if isinstance(raw, str):
            per_span = json.loads(raw)
            total_input  += per_span.get("input_tokens", 0)
            total_output += per_span.get("output_tokens", 0)

    return {"input_tokens": total_input, "output_tokens": total_output} if total_input else None


def percentile(data: list[float], p: int) -> float:
    sorted_data = sorted(data)
    idx = int(len(sorted_data) * p / 100)
    return round(sorted_data[min(idx, len(sorted_data) - 1)], 2)


def summarize(key: str) -> Optional[dict]:
    vals = [r[key] for r in results if r[key] is not None]
    if not vals:
        return None
    return {
        "avg": round(statistics.mean(vals), 2),
        "p50": percentile(vals, 50),
        "p75": percentile(vals, 75),
        "p95": percentile(vals, 95),
        "max": round(max(vals), 2),
    }

# COMMAND ----------

results = []
print("results reset")

# COMMAND ----------

if not RAG_SAMPLES:
    print("[RAG] Skipped — RAG_SAMPLES is empty")
else:
    for i, messages in enumerate(RAG_SAMPLES):
        preview = messages[-1]["content"][:60]
        print(f"[RAG {i+1}/{len(RAG_SAMPLES)}] {preview}...")
        result = profile_request(messages)
        results.append({"query": f"rag/{i+1}: {preview}", **result})
        print(f"  input={result['input_tokens']} output={result['output_tokens']} "
              f"ttft={result['ttft_ms']}ms tpot={result['tpot_ms']}ms\n")

# COMMAND ----------

if not CHAT_SAMPLES:
    print("[Chat] Skipped — CHAT_SAMPLES is empty")
else:
    for messages, label in CHAT_SAMPLES:
        preview = messages[-1]["content"][:60]
        print(f"[Chat {label}] {preview}...")
        result = profile_request(messages)
        results.append({"query": f"chat/{label}: {preview}", **result})
        print(f"  input={result['input_tokens']} output={result['output_tokens']} "
              f"ttft={result['ttft_ms']}ms tpot={result['tpot_ms']}ms\n")

# COMMAND ----------

if not TRACE_SAMPLES:
    print("[Agentic] Skipped — TRACE_SAMPLES is empty")
else:
    for trace in TRACE_SAMPLES:
        request_id = trace.get("info", {}).get("request_id", "unknown")
        usage = extract_token_usage(trace)
        if not usage:
            print(f"Skipping {request_id} — no token usage found")
            continue
        print(f"trace={request_id}  input={usage['input_tokens']}  output={usage['output_tokens']}  "
              f"total_ms={trace['info'].get('execution_time_ms', 0)}")
        results.append({
            "query":         request_id,
            "input_tokens":  usage["input_tokens"],
            "output_tokens": usage["output_tokens"],
            "ttft_ms":       None,
            "tpot_ms":       None,
            "total_ms":      trace["info"].get("execution_time_ms", 0),
        })

# COMMAND ----------

if not results:
    print("No results — fill in at least one sample list in the CUSTOMER CONFIGURATION cell.")
else:
    metrics = ["input_tokens", "output_tokens", "ttft_ms", "tpot_ms"]
    labels  = ["Input tokens", "Output tokens", "TTFT (ms)", "TPOT (ms)"]

    print(f"{'Metric':<18} {'avg':>8} {'p50':>8} {'p75':>8} {'p95':>8} {'max':>8}")
    print("-" * 58)
    for metric, label in zip(metrics, labels):
        s = summarize(metric)
        if s is None:
            continue
        print(f"{label:<18} {s['avg']:>8} {s['p50']:>8} {s['p75']:>8} {s['p95']:>8} {s['max']:>8}")

    print(f"\nn={len(results)} requests")
    print("\nUse avg input/output tokens and your QPM in the Databricks GenAI Pricing Calculator to size your PT endpoint.")

# COMMAND ----------

# MAGIC %md
# MAGIC ## PPT Capacity Ceiling for This Workload
# MAGIC
# MAGIC If you don't have historical QPM data, reverse-calculate the maximum QPM PPT can serve
# MAGIC given your measured request shape. This tells you whether PPT is sufficient before you
# MAGIC ever see production traffic.
# MAGIC
# MAGIC ```
# MAGIC max_qpm_from_itpm = PPT_ITPM_CEILING / avg_input_tokens
# MAGIC max_qpm_from_otpm = PPT_OTPM_CEILING / avg_output_tokens
# MAGIC ppt_max_qpm       = min(max_qpm_from_itpm, max_qpm_from_otpm)   <- binding constraint
# MAGIC ```
# MAGIC
# MAGIC If your expected QPM will exceed `ppt_max_qpm`, PT is required.

# COMMAND ----------

if not results:
    print("No results — run at least one workload cell first.")
else:
    avg_input  = statistics.mean(r["input_tokens"]  for r in results)
    avg_output = statistics.mean(r["output_tokens"] for r in results)

    limits = PPT_LIMITS.get(PPT_MODEL)
    if limits is None:
        print(f"Warning: {PPT_MODEL} not found in PPT_LIMITS — add it to the config cell.")
    else:
        itpm = limits["itpm"]
        otpm = limits["otpm"]
        qph  = limits["qph"]

        constraints = {
            "ITPM": itpm / avg_input,
            "OTPM": otpm / avg_output,
        }
        if qph:
            constraints["QPH"] = qph / 60

        binding     = min(constraints, key=constraints.get)
        ppt_max_qpm = constraints[binding]

        print(f"Avg input tokens/request  : {avg_input:.1f}")
        print(f"Avg output tokens/request : {avg_output:.1f}")
        print()
        print(f"  ITPM ceiling ({itpm:>7,}) : {constraints['ITPM']:.1f} QPM")
        print(f"  OTPM ceiling ({otpm:>7,}) : {constraints['OTPM']:.1f} QPM")
        if qph:
            print(f"  QPH  ceiling ({qph:>7,}) : {constraints['QPH']:.1f} QPM  ({qph:,} QPH)")
        print()
        print(f"  Binding constraint : {binding}")
        print(f"  PPT max QPM        : {ppt_max_qpm:.1f} QPM  ({ppt_max_qpm / 60:.2f} QPS)")
        print()
        print("If your expected QPM exceeds this, PPT cannot serve your load. Move to PT.")
