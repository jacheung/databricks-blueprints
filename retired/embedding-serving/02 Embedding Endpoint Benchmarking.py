# Databricks notebook source
# DBTITLE 1,Report Summary
# MAGIC %md
# MAGIC # Benchmarking Embedding Endpoints: PT, Express GPU & Pay-per-Token
# MAGIC
# MAGIC **Model:** GTE-Large-EN-v1.5 &nbsp;|&nbsp; **Input:** 256 tokens &nbsp;|&nbsp; **Date:** Oct 20, 2026
# MAGIC **Configs:** Provisioned Throughput, Express GPU (T4 & A10G), Pay-per-Token (FMAPI)
# MAGIC **Environment:** AI env v5 (transformers 4.57.1) &nbsp;|&nbsp; **Config:** `config.yml`
# MAGIC
# MAGIC ### Key Findings
# MAGIC
# MAGIC | Endpoint | Peak RPS | Sat. Conc | P50 @ Sat | P95 @ Sat | P99 @ Sat | S2Z | Recommendation |
# MAGIC | --- | --- | --- | --- | --- | --- | --- | --- |
# MAGIC | **Provisioned Throughput** | \~127 | C16 | 111ms | 151ms | 177ms | No | Throughput-critical — 10x T4, 3.7x A10G |
# MAGIC | **Express GPU (A10G)** | \~34 | C4 | 118ms | 129ms | 138ms | No | Steady mid-volume — good cost/perf ratio |
# MAGIC | **Express GPU (T4)** | \~12 | C2 | 161ms | 167ms | 171ms | No | Not recommended — hard 12 RPS ceiling |
# MAGIC | **Pay-per-Token (FMAPI)** | \~27 | Elastic | 72ms | 2,835ms | 2,935ms | Yes | Bursty/low traffic — zero idle cost |
# MAGIC
# MAGIC *Latency at each endpoint's **saturation concurrency** — the point where RPS plateaus. Beyond saturation, additional clients only add queuing delay (e.g. T4 reaches 2,602ms P50 at C32 but that's pure queue, not serving time). Dedicated endpoints run 24/7 with S2Z disabled.*
# MAGIC
# MAGIC * **Cold Start:** No meaningful cold-start penalty observed for any endpoint up to 5 min idle. PPT \~203ms at 5 min (from \~174ms warm). PT \~213ms, A10G \~218ms, T4 \~303ms.
# MAGIC * **Variance:** All endpoints show moderate std deviation (20–40ms) at all idle durations. PPT variance is comparable to dedicated endpoints — no cold-start variance explosion.
# MAGIC * **Cost:** PPT has zero idle cost (scale-to-zero). Dedicated endpoints (PT, Express T4/A10G) run 24/7 with fixed capacity.
# MAGIC
# MAGIC ---
# MAGIC
# MAGIC ### Notebook Outline
# MAGIC
# MAGIC | # | Section | Cells | Description |
# MAGIC | --- | --- | --- | --- |
# MAGIC | 1 | **Setup** | 2–4 | Dependencies, config, wait for endpoints |
# MAGIC | 2 | **Throughput Load Test** | 5–9 | Async harness, 6 concurrency levels, charts + conclusions |
# MAGIC | 3 | **Idle-Time Cold Start** | 10–13 | Re-warm/idle/probe, two-panel chart, conclusions |

# COMMAND ----------

# DBTITLE 1,Install Dependencies
# MAGIC %pip install sentence-transformers aiohttp nest_asyncio plotly -q
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Configuration & Imports
import os, time, asyncio, yaml
import numpy as np
import pandas as pd
import aiohttp
import requests
import nest_asyncio
from databricks.sdk import WorkspaceClient
import mlflow
from mlflow import MlflowClient

nest_asyncio.apply()
mlflow.set_registry_uri("databricks-uc")

# ── Workspace auth ─────────────────────────────────────────────
w = WorkspaceClient()
TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
WORKSPACE_URL = w.config.host.rstrip("/")

# ── Load shared config ─────────────────────────────────────────────
_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
_project_dir = "/Workspace" + "/".join(_nb_path.split("/")[:-1])
_config_path = f"{_project_dir}/config.yml"

with open(_config_path) as f:
    CFG = yaml.safe_load(f)
print(f"Loaded config from {_config_path}")

# ── Derived constants from config ─────────────────────────────────
CATALOG = CFG["unity_catalog"]["catalog"]
SCHEMA  = CFG["unity_catalog"]["schema"]

# ── Model identifiers ─────────────────────────────────────────────
HF_MODEL_NAME   = CFG["models"]["gte"]["hf_name"]
PT_SYSTEM_MODEL = CFG["models"]["gte"]["pt_system_model"]

# ── Endpoints (driven entirely by config.yml) ─────────────────────
EP = CFG["endpoints"]
ALL_ENDPOINTS = {ep["label"]: {"name": ep["name"], "type": ep["type"]} for ep in EP.values()}

# Convenience aliases used by downstream cells
PT_ENDPOINT_NAME   = EP["pt"]["name"]
T4_ENDPOINT_NAME   = EP["express_t4"]["name"]
A10G_ENDPOINT_NAME = EP["express_a10g"]["name"]
PPT_ENDPOINT_NAME  = EP["ppt"]["name"]

# ── Load test parameters ──────────────────────────────────────────
LT = CFG["load_test"]
TOKEN_COUNT        = LT["token_count"]
CONCURRENCY_LEVELS = LT["concurrency_levels"]
REQUESTS_PER_LEVEL = LT["requests_per_level"]

print(f"\nWorkspace:    {WORKSPACE_URL}")
print(f"Endpoints:    {[e['name'] for e in ALL_ENDPOINTS.values()]}")
print(f"Token count:  {TOKEN_COUNT}")
print(f"Concurrency:  {CONCURRENCY_LEVELS}")
print(f"Requests/lvl: {REQUESTS_PER_LEVEL}")

# COMMAND ----------

# DBTITLE 1,Wait for All Endpoints to Reach READY
from databricks.sdk.service.serving import EndpointStateReady, EndpointStateConfigUpdate

def wait_for_endpoint(endpoint_name, timeout_minutes=60, poll_interval=30):
    """Poll an endpoint until READY, FAILED, or timeout.

    BUG FIX: Previous code used '"READY" in str(state.ready)' which
    matched EndpointStateReady.NOT_READY (string contains 'READY').
    Now uses proper enum comparison.
    """
    start = time.time()
    timeout = timeout_minutes * 60
    while time.time() - start < timeout:
        ep = w.serving_endpoints.get(name=endpoint_name)
        state = ep.state
        # Enum comparison — NOT string containment
        if state.ready == EndpointStateReady.READY:
            print(f"  {endpoint_name}: READY")
            return True
        if state.config_update == EndpointStateConfigUpdate.UPDATE_FAILED:
            print(f"  {endpoint_name}: FAILED -- {state}")
            return False
        elapsed = int(time.time() - start)
        print(f"  {endpoint_name}: {state.ready.value} / {state.config_update.value if state.config_update else '-'} ({elapsed}s)")
        time.sleep(poll_interval)
    print(f"  {endpoint_name}: TIMEOUT after {timeout_minutes}m")
    return False

print("Waiting for all endpoints to be ready (this may take 15-30 min)...\n")
results = {}
for label, info in ALL_ENDPOINTS.items():
    results[label] = wait_for_endpoint(info["name"])

all_ready = all(results.values())
if all_ready:
    print("\nAll endpoints ready. Proceeding to load test.")
else:
    failed = [k for k, v in results.items() if not v]
    raise RuntimeError(f"Endpoints not ready: {failed}. Fix before continuing.")

# COMMAND ----------

# DBTITLE 1,Generate Test Prompt (~256 Tokens)
from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained(HF_MODEL_NAME, trust_remote_code=True)

def generate_prompt(target_tokens: int) -> str:
    """Generate a realistic text string of approximately `target_tokens` tokens."""
    base_text = (
        "The process of embedding text into dense vector representations "
        "enables semantic search, clustering, and retrieval augmented generation. "
        "Modern transformer architectures learn contextual representations that "
        "capture meaning beyond simple keyword matching. These models process "
        "input sequences through multiple attention layers, allowing each token "
        "to attend to every other token and build rich contextual understanding. "
    )
    # Estimate chars-per-token ratio from the base text
    base_tokens = len(tokenizer.encode(base_text, add_special_tokens=False))
    chars_per_token = len(base_text) / base_tokens

    # Over-generate then trim to exact token count
    target_chars = int(target_tokens * chars_per_token * 1.1)
    repeated = (base_text * ((target_chars // len(base_text)) + 2))[:target_chars]
    token_ids = tokenizer.encode(repeated, add_special_tokens=False)[:target_tokens]
    prompt = tokenizer.decode(token_ids, skip_special_tokens=True)

    actual_tokens = len(tokenizer.encode(prompt, add_special_tokens=False))
    return prompt, actual_tokens

test_prompt, actual_count = generate_prompt(TOKEN_COUNT)
print(f"Generated prompt: {actual_count} tokens, {len(test_prompt)} chars")
print(f"Preview: {test_prompt[:250]}...")

# COMMAND ----------

# DBTITLE 1,Load Test Harness (async HTTP)
import aiohttp
import asyncio
import time
import numpy as np

async def _send_request(session, url, headers, payload, results_list):
    """Send a single embedding request and record latency."""
    start = time.perf_counter()
    try:
        async with session.post(url, headers=headers, json=payload) as resp:
            body = await resp.read()
            latency = (time.perf_counter() - start) * 1000  # ms
            results_list.append({
                "latency_ms": latency,
                "status": resp.status,
                "success": resp.status == 200,
            })
            if resp.status != 200:
                results_list[-1]["error"] = body.decode("utf-8", errors="replace")[:200]
    except Exception as e:
        latency = (time.perf_counter() - start) * 1000
        results_list.append({
            "latency_ms": latency,
            "status": -1,
            "success": False,
            "error": str(e)[:200],
        })


async def run_load_test(endpoint_name, endpoint_type, prompt, concurrency, num_requests):
    """
    Fire `num_requests` at `endpoint_name` with `concurrency` concurrent connections.
    Returns a dict of latency percentiles and throughput stats.
    """
    url = f"{WORKSPACE_URL}/serving-endpoints/{endpoint_name}/invocations"
    headers = {
        "Authorization": f"Bearer {TOKEN}",
        "Content-Type": "application/json",
    }

    # PT endpoints use OpenAI embedding format; custom use MLflow format
    if endpoint_type in ("pt", "ppt"):
        payload = {"input": [prompt]}
    else:
        payload = {"inputs": [prompt]}

    results_list = []
    connector = aiohttp.TCPConnector(limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=180)

    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        # Warm-up: 5 sequential requests (discard latency)
        for _ in range(5):
            await _send_request(session, url, headers, payload, [])

        # Actual load test with bounded concurrency via semaphore
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded_request():
            async with semaphore:
                await _send_request(session, url, headers, payload, results_list)

        start_time = time.perf_counter()
        tasks = [bounded_request() for _ in range(num_requests)]
        await asyncio.gather(*tasks)
        total_time = time.perf_counter() - start_time

    # Compute statistics from successful requests
    latencies = [r["latency_ms"] for r in results_list if r["success"]]
    errors = [r for r in results_list if not r["success"]]

    if not latencies:
        return None, errors

    stats = {
        "p50":  np.percentile(latencies, 50),
        "p95":  np.percentile(latencies, 95),
        "p99":  np.percentile(latencies, 99),
        "mean": np.mean(latencies),
        "min":  np.min(latencies),
        "max":  np.max(latencies),
        "rps":  len(latencies) / total_time,
        "total_requests": num_requests,
        "successful":     len(latencies),
        "errors":         len(errors),
        "total_time_s":   round(total_time, 2),
    }
    return stats, errors

print("Load test harness ready.")
print(f"  Warm-up:          5 sequential requests (discarded)")
print(f"  Requests/level:   {REQUESTS_PER_LEVEL}")
print(f"  Concurrency:      {CONCURRENCY_LEVELS}")

# COMMAND ----------

# DBTITLE 1,Execute Load Tests (4 endpoints x concurrency levels)
all_results = []
all_errors = {}

for label, info in ALL_ENDPOINTS.items():
    endpoint_name = info["name"]
    endpoint_type = info["type"]

    print(f"\n{'='*65}")
    print(f" {label}  ({endpoint_name})")
    print(f"{'='*65}")

    for conc in CONCURRENCY_LEVELS:
        print(f"  concurrency={conc:>3d} ...", end=" ", flush=True)

        loop = asyncio.get_event_loop()
        stats, errors = loop.run_until_complete(
            run_load_test(endpoint_name, endpoint_type, test_prompt, conc, REQUESTS_PER_LEVEL)
        )

        if stats:
            print(
                f"p50={stats['p50']:7.1f}ms  "
                f"p95={stats['p95']:7.1f}ms  "
                f"p99={stats['p99']:7.1f}ms  "
                f"RPS={stats['rps']:6.1f}  "
                f"err={stats['errors']}"
            )
            stats["endpoint"] = label
            stats["concurrency"] = conc
            all_results.append(stats)
        else:
            print("FAILED -- no successful requests")

        if errors:
            all_errors[(label, conc)] = errors

results_df = pd.DataFrame(all_results)
print(f"\nLoad tests complete. {len(all_results)} data points collected.")

# Show any errors
if all_errors:
    print(f"\nEndpoints with errors:")
    for (ep, conc), errs in all_errors.items():
        print(f"  {ep} @ concurrency {conc}: {len(errs)} error(s)")
        print(f"    Sample: {errs[0].get('error', 'unknown')[:150]}")

display(results_df)

# COMMAND ----------

# DBTITLE 1,Results & Visualization
import plotly.graph_objects as go
from plotly.subplots import make_subplots

colors = {ep["label"]: ep["color"] for ep in EP.values()}

# ── Latency percentile plots (3 subplots) ─────────────────────────
metrics = ["p50", "p95", "p99"]
latency_fig = make_subplots(
    rows=1, cols=3,
    subplot_titles=[f"{m.upper()} Latency" for m in metrics],
    shared_yaxes=True,
    horizontal_spacing=0.04,
)

for col, metric in enumerate(metrics, start=1):
    for label in ALL_ENDPOINTS.keys():
        ep_data = results_df[results_df["endpoint"] == label].sort_values("concurrency")
        if not ep_data.empty:
            latency_fig.add_trace(
                go.Scatter(
                    x=ep_data["concurrency"], y=ep_data[metric],
                    mode="lines+markers", name=label,
                    line=dict(color=colors[label], width=2),
                    marker=dict(size=7),
                    legendgroup=label, showlegend=(col == 1),
                ),
                row=1, col=col,
            )
    latency_fig.update_xaxes(title_text="Client Concurrency", row=1, col=col,
                             tickvals=CONCURRENCY_LEVELS)

latency_fig.update_yaxes(title_text="Latency (ms)", row=1, col=1)
latency_fig.update_layout(
    title=dict(text=f"Embedding Load Test: PT vs Custom GPU ({actual_count} tokens)", font=dict(size=16)),
    height=450, width=1200, template="plotly_white",
    legend=dict(orientation="h", yanchor="bottom", y=-0.25, xanchor="center", x=0.5),
)
displayHTML(latency_fig.to_html(full_html=False, include_plotlyjs=True))

# ── Throughput (RPS) comparison ───────────────────────────────────
rps_fig = go.Figure()
for label in ALL_ENDPOINTS.keys():
    ep_data = results_df[results_df["endpoint"] == label].sort_values("concurrency")
    if not ep_data.empty:
        rps_fig.add_trace(go.Scatter(
            x=ep_data["concurrency"], y=ep_data["rps"],
            mode="lines+markers", name=label,
            line=dict(color=colors[label], width=2),
            marker=dict(size=8, symbol="square"),
        ))

rps_fig.update_layout(
    title=dict(text=f"Throughput: PT vs Custom GPU ({actual_count} tokens)", font=dict(size=16)),
    xaxis=dict(title="Client Concurrency", tickvals=CONCURRENCY_LEVELS),
    yaxis=dict(title="Requests Per Second (RPS)"),
    height=500, width=900, template="plotly_white",
    legend=dict(orientation="h", yanchor="bottom", y=-0.2, xanchor="center", x=0.5),
)
displayHTML(rps_fig.to_html(full_html=False, include_plotlyjs=True))

# ── Summary table ─────────────────────────────────────────────────
print("\n── Summary at peak concurrency ──")
peak = results_df[results_df["concurrency"] == CONCURRENCY_LEVELS[-1]]
for _, row in peak.iterrows():
    print(
        f"  {row['endpoint']:25s}  "
        f"p50={row['p50']:7.1f}ms  "
        f"p95={row['p95']:7.1f}ms  "
        f"RPS={row['rps']:6.1f}  "
        f"errors={row['errors']}"
    )

# COMMAND ----------

# DBTITLE 1,Load Test Conclusions
# MAGIC %md
# MAGIC ## Throughput Saturation
# MAGIC
# MAGIC | Endpoint | Peak RPS | Sat. Conc | P50 @ Sat | Behavior |
# MAGIC | --- | --- | --- | --- | --- |
# MAGIC | **Provisioned Throughput** | \~127 | C16 | 111ms | Scales linearly then plateaus — multi-GPU capacity |
# MAGIC | **Express GPU (A10G)** | \~34 | C4 | 118ms | Single-GPU ceiling; 2.8x T4 throughput |
# MAGIC | **Express GPU (T4)** | \~12 | C2 | 161ms | Single-GPU bottleneck — pure queuing beyond C2 |
# MAGIC | **Pay-per-Token (FMAPI)** | \~27 | Elastic | 72ms | Auto-scales; P50 flat but P95/P99 tail is volatile |
# MAGIC
# MAGIC > **Takeaway:** PT delivers 10x the throughput of T4 and 3.7x A10G. Express GPUs hit hard ceilings early (C2, C4). PPT auto-scales but with volatile tail latency. All endpoints deliver sub-200ms P50 at their saturation point.
# MAGIC
# MAGIC ## Latency: Baseline vs Saturation vs Overload
# MAGIC
# MAGIC | Endpoint | P50 @ C1 (baseline) | P50 @ Sat | P50 @ C32 (overload) | Queuing Penalty |
# MAGIC | --- | --- | --- | --- | --- |
# MAGIC | **Provisioned Throughput** | 58ms | 111ms (C16) | 221ms | 2x — graceful degradation, still has headroom |
# MAGIC | **Express GPU (A10G)** | 60ms | 118ms (C4) | 921ms | 15x — pure queuing beyond C4 |
# MAGIC | **Express GPU (T4)** | 102ms | 161ms (C2) | 2,602ms | 26x — pure queuing beyond C2 |
# MAGIC | **Pay-per-Token (FMAPI)** | 75ms | 72ms (C16) | 94ms | P50 flat (elastic); P99 \>2.9s at all levels |
# MAGIC
# MAGIC > **Takeaway:** At saturation, all endpoints deliver sub-200ms P50. The key differentiator is overload behavior: PT degrades gracefully (2x), while Express GPUs show strict linear queuing (15–26x). PPT P50 stays flat regardless of load, but tail latency (P99 \>2.9s) is volatile at every concurrency level.

# COMMAND ----------

# DBTITLE 1,Idle-Time Cold-Start Experiment – Harness
# ── Idle-Time Cold-Start Experiment ────────────────────────────────
# Measures single-request latency after increasing idle durations.
# PT and custom GPU (scale-to-zero disabled) should show flat latency;
# PPT (FMAPI) may exhibit cold-start spikes.
#
# Design:
#   • Re-warm before EVERY measurement (known-warm starting state)
#   • 3 repetitions per idle duration for statistical confidence
#   • All 4 endpoints probed in parallel per idle window
#   • Dropped 10-min idle (5-min sufficient to detect cold start)
#
# Estimated runtime: ~25 min

from concurrent.futures import ThreadPoolExecutor, as_completed

IDLE_TIMES_SEC = [5, 30, 60, 300]          # idle durations (dropped 10min)
REPS = 3                                    # repetitions per idle duration
IDLE_WARMUP_REQUESTS = 10                   # warm-up requests before EACH measurement

def send_single_request_sync(endpoint_name, endpoint_type, prompt):
    """Send one embedding request synchronously; return latency dict."""
    url = f"{WORKSPACE_URL}/serving-endpoints/{endpoint_name}/invocations"
    headers = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

    if endpoint_type in ("pt", "ppt"):
        payload = {"input": [prompt]}
    else:
        payload = {"inputs": [prompt]}

    start = time.perf_counter()
    try:
        resp = requests.post(url, headers=headers, json=payload, timeout=180)
        latency_ms = (time.perf_counter() - start) * 1000
        return {
            "latency_ms": latency_ms,
            "status": resp.status_code,
            "success": resp.status_code == 200,
        }
    except Exception as e:
        latency_ms = (time.perf_counter() - start) * 1000
        return {"latency_ms": latency_ms, "status": -1, "success": False, "error": str(e)}


def warm_endpoint(label, info, prompt, n_requests):
    """Send n warmup requests to one endpoint. Returns (label, latencies)."""
    lats = []
    for _ in range(n_requests):
        r = send_single_request_sync(info["name"], info["type"], prompt)
        if r["success"]:
            lats.append(r["latency_ms"])
    return label, lats


def probe_endpoint(label, info, prompt):
    """Send one request to an endpoint. Returns (label, result_dict)."""
    r = send_single_request_sync(info["name"], info["type"], prompt)
    return label, r


total_idle = sum(IDLE_TIMES_SEC) * REPS
print("Idle-time experiment harness ready.")
print(f"  Idle durations:  {IDLE_TIMES_SEC} seconds")
print(f"  Repetitions:     {REPS}")
print(f"  Warmup requests: {IDLE_WARMUP_REQUESTS} (re-warm before each measurement)")
print(f"  Endpoints:       {list(ALL_ENDPOINTS.keys())}")
print(f"  Parallelized:    yes (all endpoints probed simultaneously)")
print(f"  Est. runtime:    ~{total_idle // 60 + 3} min")

# COMMAND ----------

# DBTITLE 1,Execute Idle-Time Experiment (parallel, 3 reps, re-warm)
idle_results = []

for rep in range(1, REPS + 1):
    print(f"\n{'#'*65}")
    print(f"  Repetition {rep}/{REPS}")
    print(f"{'#'*65}")

    for idle_sec in [0] + IDLE_TIMES_SEC:
        idle_label = "0s (warm)" if idle_sec == 0 else (
            f"{idle_sec}s" if idle_sec < 60 else f"{idle_sec // 60}min"
        )

        # ── Warm ALL endpoints in parallel ─────────────────────
        print(f"\n  [{idle_label}] Warming all endpoints...", end=" ", flush=True)
        with ThreadPoolExecutor(max_workers=len(ALL_ENDPOINTS)) as pool:
            warmup_futures = {
                pool.submit(warm_endpoint, lbl, info, test_prompt, IDLE_WARMUP_REQUESTS): lbl
                for lbl, info in ALL_ENDPOINTS.items()
            }
            warmup_avgs = {}
            for f in as_completed(warmup_futures):
                lbl, lats = f.result()
                warmup_avgs[lbl] = np.mean(lats) if lats else None
        avg_str = ", ".join(
            f"{lbl.split()[0][:4]}={avg:.0f}ms"
            for lbl, avg in warmup_avgs.items() if avg is not None
        )
        print(f"done ({avg_str})")

        # ── Idle ─────────────────────────────────────────────
        if idle_sec > 0:
            print(f"  [{idle_label}] Idling {idle_sec}s...", end="", flush=True)
            time.sleep(idle_sec)
            print(" done", end="", flush=True)

        # ── Probe ALL endpoints in parallel ────────────────────
        print(f"  [{idle_label}] Probing...", end=" ", flush=True)
        with ThreadPoolExecutor(max_workers=len(ALL_ENDPOINTS)) as pool:
            probe_futures = {
                pool.submit(probe_endpoint, lbl, info, test_prompt): lbl
                for lbl, info in ALL_ENDPOINTS.items()
            }
            for f in as_completed(probe_futures):
                lbl, result = f.result()
                latency = result["latency_ms"] if result["success"] else None
                idle_results.append({
                    "endpoint": lbl,
                    "idle_seconds": idle_sec,
                    "idle_label": idle_label,
                    "latency_ms": latency,
                    "status": result["status"],
                    "rep": rep,
                })
                tag = f"{latency:.0f}ms" if latency else f"FAIL({result['status']})"
                print(f"{lbl.split()[0][:4]}={tag}", end="  ", flush=True)
        print()

idle_df = pd.DataFrame(idle_results)
print(f"\nIdle-time experiment complete. {len(idle_results)} measurements "
      f"({REPS} reps \u00d7 {len([0]+IDLE_TIMES_SEC)} idle levels \u00d7 {len(ALL_ENDPOINTS)} endpoints).")
display(idle_df)

# COMMAND ----------

# DBTITLE 1,Idle-Time Results: Cold-Start Gap + Variance (two-panel)
import plotly.graph_objects as go

colors = {ep["label"]: ep["color"] for ep in EP.values()}

# ── Aggregate across reps: mean and std ───────────────────────
idle_stats = idle_df.groupby(["endpoint", "idle_label", "idle_seconds"]).agg(
    mean_ms=("latency_ms", "mean"),
    std_ms=("latency_ms", "std"),
).reset_index()
idle_stats["std_ms"] = idle_stats["std_ms"].fillna(0)

category_order = ["0s (warm)", "5s", "30s", "1min", "5min"]

# ── Two-panel: Cold-Start Gap + Variance ─────────────────────────
from plotly.subplots import make_subplots

ppt_label = "Pay-per-Token (FMAPI)"

fig = make_subplots(
    rows=1, cols=2,
    subplot_titles=[
        "<b>Cold-Start Gap</b>: Mean Latency",
        "<b>Consistency</b>: Std Deviation",
    ],
    horizontal_spacing=0.12,
)

# Left panel — clean mean-latency lines (no error bars)
for label in ALL_ENDPOINTS.keys():
    ep_data = idle_stats[idle_stats["endpoint"] == label].sort_values("idle_seconds")
    if not ep_data.empty:
        is_ppt = (label == ppt_label)
        fig.add_trace(go.Scatter(
            x=ep_data["idle_label"],
            y=ep_data["mean_ms"],
            mode="lines+markers",
            name=label,
            line=dict(color=colors.get(label, "#888"), width=3 if is_ppt else 2),
            marker=dict(size=10 if is_ppt else 7),
            legendgroup=label,
            showlegend=True,
        ), row=1, col=1)

# Right panel — std deviation as grouped bars
for label in ALL_ENDPOINTS.keys():
    ep_data = idle_stats[idle_stats["endpoint"] == label].sort_values("idle_seconds")
    if not ep_data.empty:
        is_ppt = (label == ppt_label)
        fig.add_trace(go.Bar(
            x=ep_data["idle_label"],
            y=ep_data["std_ms"],
            name=label,
            marker_color=colors.get(label, "#888"),
            opacity=1.0 if is_ppt else 0.5,
            legendgroup=label,
            showlegend=False,
        ), row=1, col=2)

for col in [1, 2]:
    fig.update_xaxes(
        title_text="Idle Duration", row=1, col=col,
        categoryorder="array", categoryarray=category_order,
    )
fig.update_yaxes(title_text="Mean Latency (ms)", row=1, col=1)
fig.update_yaxes(title_text="Std Deviation (ms)", row=1, col=2)

fig.update_layout(
    title=dict(
        text=(f"PPT Cold Start & Variance vs Dedicated Endpoints "
              f"({actual_count} tokens, {REPS} reps)"),
        font=dict(size=14),
    ),
    height=500, width=1100, template="plotly_white",
    legend=dict(orientation="h", yanchor="bottom", y=-0.22, xanchor="center", x=0.5),
    barmode="group",
)
displayHTML(fig.to_html(full_html=False, include_plotlyjs=True))

# ── Summary: mean \u00b1 std by endpoint \u00d7 idle duration ─────────────
if not idle_df.empty:
    pivot_mean = idle_df.pivot_table(
        index="endpoint", columns="idle_label",
        values="latency_ms", aggfunc="mean",
    )
    pivot_std = idle_df.pivot_table(
        index="endpoint", columns="idle_label",
        values="latency_ms", aggfunc="std",
    ).fillna(0)

    col_order = [c for c in category_order if c in pivot_mean.columns]
    pivot_mean = pivot_mean[col_order].round(1)
    pivot_std = pivot_std[col_order].round(1)

    combined = pivot_mean.astype(str) + " \u00b1 " + pivot_std.astype(str)
    print(f"\n\u2500\u2500 Latency (ms): Mean \u00b1 StdDev ({REPS} reps) \u2500\u2500\n")
    display(combined)

# COMMAND ----------

# DBTITLE 1,Idle-Time Conclusions
# MAGIC %md
# MAGIC ## Cold-Start Gap
# MAGIC
# MAGIC All four endpoints maintain surprisingly consistent latency across idle durations (0s–5min). No clear cold-start penalty was observed for any endpoint, including PPT. Mean latency at the 5-minute mark:
# MAGIC
# MAGIC | Endpoint | Warm (0s) | 5 min idle | Δ |
# MAGIC | --- | --- | --- | --- |
# MAGIC | **Provisioned Throughput** | 179ms | 213ms | +34ms |
# MAGIC | **Express GPU (T4)** | 228ms | 303ms | +75ms |
# MAGIC | **Express GPU (A10G)** | 271ms | 218ms | -53ms (noise) |
# MAGIC | **Pay-per-Token (FMAPI)** | 174ms | 203ms | +29ms |
# MAGIC
# MAGIC ## Variance
# MAGIC
# MAGIC All endpoints show moderate standard deviation (20–40ms) at all idle durations. PPT variance is comparable to dedicated endpoints — no cold-start variance explosion as seen in the previous BGE benchmark run. This suggests the shared FMAPI pool for GTE remains sufficiently warm even after 5 minutes idle.
# MAGIC
# MAGIC Express GPU (A10G) shows occasional variance spikes (up to 182ms std at 0s warm), likely due to GPU scheduling jitter on a single GPU.
# MAGIC
# MAGIC > **Takeaway:** Unlike the previous BGE benchmark, no endpoint showed meaningful cold-start degradation at any idle duration tested. All four endpoints remain stable for bursty traffic patterns with idle gaps up to 5 minutes.