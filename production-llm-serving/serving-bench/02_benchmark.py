# Databricks notebook source
# DBTITLE 1,Benchmark serving endpoints
# MAGIC %md
# MAGIC # Benchmark serving endpoints
# MAGIC
# MAGIC Runs against every endpoint in the config (`configs/<model>.yml`) (deployed by `01_deploy`):
# MAGIC
# MAGIC 1. **Concurrency sweep** — closed loop: at each concurrency level, N requests with at most C in
# MAGIC    flight. Measures achieved RPS and latency percentiles; it does not schedule a target arrival rate.
# MAGIC 2. **Idle test** — warm, wait, then one probe per endpoint, repeated. Measures latency after
# MAGIC    client inactivity; it can't separate a cold start from shared-pool load or connection setup.
# MAGIC
# MAGIC Every request (successes and failures) is appended to `{catalog}.{schema}.serving_bench_requests`
# MAGIC and per-level stats to `serving_bench_summary`, keyed by `run_id`. The notebook exits with the
# MAGIC readout table as JSON so a job caller can fetch it with `databricks jobs get-run-output`.
# MAGIC
# MAGIC Requests per level scale with concurrency (`max(requests_per_level, requests_per_client × C)`) so
# MAGIC high-concurrency windows aren't a fraction of a second. Afterwards, dedicated endpoints this framework
# MAGIC created are deleted (`cleanup.delete_endpoints_after_benchmark`, default true).
# MAGIC
# MAGIC Chat latency is the full generated response (`stream: false`), not time to first token.
# MAGIC
# MAGIC **Compute:** Serverless CPU is enough; no model weights are loaded.

# COMMAND ----------

# DBTITLE 1,Install dependencies
# MAGIC %pip install "transformers>=4.40,<5" aiohttp nest_asyncio plotly pyyaml -q

# COMMAND ----------

dbutils.library.restartPython()

# COMMAND ----------

# DBTITLE 1,Load config
import asyncio
import json
import sys
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import aiohttp
import nest_asyncio
import numpy as np
import pandas as pd
import requests
from databricks.sdk import WorkspaceClient

nest_asyncio.apply()

_nb_path = dbutils.notebook.entry_point.getDbutils().notebook().getContext().notebookPath().get()
PROJECT_DIR = "/Workspace" + "/".join(_nb_path.split("/")[:-1])
sys.path.insert(0, PROJECT_DIR)
from common import (endpoint_label, load_config, parse_response, readout, readout_document, readout_markdown,
                    request_payload, requests_for_level, wait_for_endpoint)

dbutils.widgets.text("config_path", "")  # e.g. {PROJECT_DIR}/configs/<model>.yml
if not dbutils.widgets.get("config_path"):
    raise ValueError(f"Set the config_path widget, e.g. {PROJECT_DIR}/configs/<model>.yml")
CFG = load_config(dbutils.widgets.get("config_path"))

w = WorkspaceClient()
TOKEN = dbutils.notebook.entry_point.getDbutils().notebook().getContext().apiToken().get()
WORKSPACE_URL = w.config.host.rstrip("/")
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

MODEL = CFG["model"]
TASK = MODEL["task"]
UC = CFG["unity_catalog"]
LT = CFG["load_test"]
IDLE = CFG.get("idle_test") or {"idle_seconds": []}
ENDPOINTS = {endpoint_label(ep): ep for ep in CFG["endpoints"]}
if len(ENDPOINTS) != len(CFG["endpoints"]):
    raise ValueError("Endpoint labels must be unique; set `label` on duplicates in the config")

RUN_ID = f"run_{uuid.uuid4().hex[:12]}"
STARTED_AT = datetime.now(timezone.utc)
print(f"Run {RUN_ID}: {MODEL['hf_name']} ({TASK}) on {list(ENDPOINTS)}")

not_ready = [ep["name"] for ep in ENDPOINTS.values() if not wait_for_endpoint(w, ep["name"])]
if not_ready:
    raise RuntimeError(f"Endpoints not ready: {not_ready}. Run 01_deploy first.")

# COMMAND ----------

# DBTITLE 1,Generate the test prompt (exact token count)
from transformers import AutoTokenizer

tokenizer = AutoTokenizer.from_pretrained(MODEL["hf_name"], trust_remote_code=MODEL.get("trust_remote_code", False))
BASE_TEXT = (
    "The process of embedding text into dense vector representations enables semantic search, "
    "clustering, and retrieval augmented generation. Modern transformer architectures learn "
    "contextual representations that capture meaning beyond simple keyword matching. "
)
_ids = tokenizer.encode(BASE_TEXT * (LT["input_tokens"] // 20 + 2), add_special_tokens=False)
PROMPT = tokenizer.decode(_ids[: LT["input_tokens"]], skip_special_tokens=True)
PROMPT_TOKENS = len(tokenizer.encode(PROMPT, add_special_tokens=False))
print(f"Prompt: {PROMPT_TOKENS} tokens, {len(PROMPT)} chars")

# COMMAND ----------

# DBTITLE 1,Load test harness (async HTTP)
def record(ep, label, phase, latency_ms, status, body=None, error=None, **extra):
    row = {"run_id": RUN_ID, "model": MODEL["hf_name"], "task": TASK, "endpoint": label,
           "endpoint_name": ep["name"], "method": ep["method"], "phase": phase,
           "latency_ms": latency_ms, "status": status, "success": False, "error": error,
           "prompt_tokens": None, "completion_tokens": None, "concurrency": None,
           "idle_seconds": None, "rep": None, "ts": datetime.now(timezone.utc)} | extra
    if status == 200 and error is None:
        try:
            parsed = parse_response(TASK, body)
            row["success"] = True
            row["prompt_tokens"] = parsed.get("prompt_tokens")
            row["completion_tokens"] = parsed.get("completion_tokens")
        except ValueError as e:
            row["error"] = str(e)[:200]
    return row


async def send(session, ep, label, phase, **extra):
    url = f"{WORKSPACE_URL}/serving-endpoints/{ep['name']}/invocations"
    payload = request_payload(ep["method"], TASK, PROMPT, LT.get("max_tokens", 128))
    start = time.perf_counter()
    try:
        async with session.post(url, headers=HEADERS, json=payload) as resp:
            text = await resp.text()
            latency = (time.perf_counter() - start) * 1000
            if resp.status != 200:
                return record(ep, label, phase, latency, resp.status, error=text[:200], **extra)
            return record(ep, label, phase, latency, 200, body=json.loads(text), **extra)
    except Exception as e:
        return record(ep, label, phase, (time.perf_counter() - start) * 1000, -1, error=str(e)[:200], **extra)


async def run_level(ep, label, concurrency):
    """Warmups (sequential, saved but excluded from stats), then N requests with <= C in flight."""
    connector = aiohttp.TCPConnector(limit=concurrency)
    timeout = aiohttp.ClientTimeout(total=LT["timeout_seconds"])
    async with aiohttp.ClientSession(connector=connector, timeout=timeout) as session:
        rows = [await send(session, ep, label, "warmup", concurrency=concurrency)
                for _ in range(LT["warmup_requests"])]
        semaphore = asyncio.Semaphore(concurrency)

        async def bounded():
            async with semaphore:
                return await send(session, ep, label, "load", concurrency=concurrency)

        start = time.perf_counter()
        measured = await asyncio.gather(*(bounded() for _ in range(requests_for_level(LT, concurrency))))
        duration = time.perf_counter() - start
    ok = [r["latency_ms"] for r in measured if r["success"]]
    stats = {"run_id": RUN_ID, "model": MODEL["hf_name"], "task": TASK, "endpoint": label,
             "endpoint_name": ep["name"], "method": ep["method"], "concurrency": concurrency,
             "requests": len(measured), "successes": len(ok), "errors": len(measured) - len(ok),
             "rate_limited": sum(r["status"] == 429 for r in measured),
             "rps": len(ok) / duration, "duration_s": duration, "input_tokens": PROMPT_TOKENS}
    for p in (50, 95, 99):
        stats[f"p{p}"] = float(np.percentile(ok, p)) if ok else None
    return rows + list(measured), stats

# COMMAND ----------

# DBTITLE 1,Run the concurrency sweep
request_rows, summary_rows = [], []
loop = asyncio.get_event_loop()
for label, ep in ENDPOINTS.items():
    print(f"\n{label} ({ep['name']})")
    for c in LT["concurrency_levels"]:
        rows, stats = loop.run_until_complete(run_level(ep, label, c))
        request_rows += rows
        summary_rows.append(stats)
        p50 = f"{stats['p50']:7.1f}ms" if stats["p50"] is not None else "      —"
        print(f"  C={c:>3}  p50={p50}  RPS={stats['rps']:6.1f}  errors={stats['errors']}")
        if stats["errors"]:
            print("    sample error:", next(r["error"] for r in rows if r["phase"] == "load" and not r["success"]))

summary_df = pd.DataFrame(summary_rows)
display(summary_df)

# COMMAND ----------

# DBTITLE 1,Sweep charts
import plotly.express as px
import plotly.graph_objects as go
from plotly.subplots import make_subplots

COLORS = dict(zip(ENDPOINTS, px.colors.qualitative.Plotly))
levels = LT["concurrency_levels"]

fig = make_subplots(rows=1, cols=4, subplot_titles=["Throughput (RPS)", "P50 latency", "P95 latency", "P99 latency"],
                    horizontal_spacing=0.06)
for label in ENDPOINTS:
    d = summary_df[summary_df["endpoint"] == label].sort_values("concurrency")
    for col, metric in enumerate(["rps", "p50", "p95", "p99"], start=1):
        fig.add_trace(go.Scatter(x=d["concurrency"], y=d[metric], mode="lines+markers", name=label,
                                 line=dict(color=COLORS[label]), legendgroup=label, showlegend=col == 1),
                      row=1, col=col)
        fig.update_xaxes(title_text="Client concurrency", type="log", tickvals=levels, row=1, col=col)
fig.update_yaxes(title_text="Requests/s", row=1, col=1)
fig.update_yaxes(title_text="Latency (ms)", row=1, col=2)
fig.update_layout(title=f"{MODEL['hf_name']} — {PROMPT_TOKENS}-token input", height=420, width=1400,
                  template="plotly_white", legend=dict(orientation="h", y=-0.25, x=0.5, xanchor="center"))
displayHTML(fig.to_html(full_html=False, include_plotlyjs=True))

# COMMAND ----------

# DBTITLE 1,Idle test (warm → wait → probe, all endpoints in parallel)
def send_sync(ep, label, phase, **extra):
    payload = request_payload(ep["method"], TASK, PROMPT, LT.get("max_tokens", 128))
    start = time.perf_counter()
    try:
        resp = requests.post(f"{WORKSPACE_URL}/serving-endpoints/{ep['name']}/invocations",
                             headers=HEADERS, json=payload, timeout=LT["timeout_seconds"])
        latency = (time.perf_counter() - start) * 1000
        if resp.status_code != 200:
            return record(ep, label, phase, latency, resp.status_code, error=resp.text[:200], **extra)
        return record(ep, label, phase, latency, 200, body=resp.json(), **extra)
    except Exception as e:
        return record(ep, label, phase, (time.perf_counter() - start) * 1000, -1, error=str(e)[:200], **extra)


def warm_wait_probe(label, idle_seconds, rep):
    ep = ENDPOINTS[label]
    rows = [send_sync(ep, label, "idle_warmup", idle_seconds=idle_seconds, rep=rep)
            for _ in range(IDLE["warmup_requests"])]
    time.sleep(idle_seconds)
    return rows + [send_sync(ep, label, "idle_probe", idle_seconds=idle_seconds, rep=rep)]


idle_rows = []
if IDLE["idle_seconds"]:
    print(f"Idle test: ~{sum(IDLE['idle_seconds']) * IDLE['reps'] // 60 + 2} min")
    for rep in range(1, IDLE["reps"] + 1):
        for idle_seconds in [0] + IDLE["idle_seconds"]:
            # Each endpoint gets its own warm → wait → probe so no benchmark traffic lands in its idle window.
            with ThreadPoolExecutor(max_workers=len(ENDPOINTS)) as pool:
                for rows in pool.map(lambda label: warm_wait_probe(label, idle_seconds, rep), ENDPOINTS):
                    idle_rows += rows
            probes = [r for r in idle_rows if r["phase"] == "idle_probe" and r["rep"] == rep
                      and r["idle_seconds"] == idle_seconds]
            print(f"  rep {rep} idle {idle_seconds:>4}s: " + "  ".join(
                f"{r['endpoint']}={r['latency_ms']:.0f}ms" if r["success"] else f"{r['endpoint']}=FAIL({r['status']})"
                for r in probes))
request_rows += idle_rows

# COMMAND ----------

# DBTITLE 1,Idle charts
probe_df = pd.DataFrame([r for r in idle_rows if r["phase"] == "idle_probe"])
if not probe_df.empty:
    probe_df["ok_latency_ms"] = probe_df["latency_ms"].where(probe_df["success"])
    idle_stats = (probe_df.groupby(["endpoint", "idle_seconds"])["ok_latency_ms"]
                  .agg(mean_ms="mean", std_ms="std").reset_index().fillna({"std_ms": 0}))
    fig = make_subplots(rows=1, cols=2, subplot_titles=["Mean latency after idle", "Std deviation"])
    for label in ENDPOINTS:
        d = idle_stats[idle_stats["endpoint"] == label].sort_values("idle_seconds")
        fig.add_trace(go.Scatter(x=d["idle_seconds"].astype(str) + "s", y=d["mean_ms"], mode="lines+markers",
                                 name=label, line=dict(color=COLORS[label]), legendgroup=label), row=1, col=1)
        fig.add_trace(go.Bar(x=d["idle_seconds"].astype(str) + "s", y=d["std_ms"], name=label,
                             marker_color=COLORS[label], legendgroup=label, showlegend=False), row=1, col=2)
    fig.update_xaxes(title_text="Idle duration")
    fig.update_yaxes(title_text="Latency (ms)")
    fig.update_layout(height=420, width=1100, template="plotly_white", barmode="group",
                      legend=dict(orientation="h", y=-0.25, x=0.5, xanchor="center"))
    displayHTML(fig.to_html(full_html=False, include_plotlyjs=True))
    display(idle_stats.pivot(index="endpoint", columns="idle_seconds", values="mean_ms").round(1))

# COMMAND ----------

# DBTITLE 1,Save results to Unity Catalog
TABLE_PREFIX = f"`{UC['catalog']}`.`{UC['schema']}`"
requests_df = pd.DataFrame(request_rows)
# Explicit dtypes: an all-null column would otherwise reach Spark as a void type Delta can't store.
for col in ("concurrency", "idle_seconds", "rep", "prompt_tokens", "completion_tokens"):
    requests_df[col] = requests_df[col].astype("Int64")
requests_df["error"] = requests_df["error"].fillna("").astype(str)
summary_df[["p50", "p95", "p99"]] = summary_df[["p50", "p95", "p99"]].astype(float)
requests_df["started_at"] = STARTED_AT
summary_df["started_at"] = STARTED_AT

spark.createDataFrame(requests_df).write.mode("append").option("mergeSchema", "true") \
    .saveAsTable(f"{TABLE_PREFIX}.serving_bench_requests")
spark.createDataFrame(summary_df).write.mode("append").option("mergeSchema", "true") \
    .saveAsTable(f"{TABLE_PREFIX}.serving_bench_summary")
print(f"Saved {len(requests_df)} requests and {len(summary_df)} summary rows for {RUN_ID} to {TABLE_PREFIX}")

# COMMAND ----------

# DBTITLE 1,Delete dedicated endpoints this framework created
# Dedicated endpoints bill until deleted; serving endpoints can't be stopped. Only pt/custom endpoints
# tagged created_by=serving-bench are deleted — never shared ppt or pre-existing endpoints you pointed at.
deleted = []
if (CFG.get("cleanup") or {}).get("delete_endpoints_after_benchmark", True):
    for ep in ENDPOINTS.values():
        if ep["method"] == "ppt":
            continue
        tags = {t.key: t.value for t in (w.serving_endpoints.get(name=ep["name"]).tags or [])}
        if tags.get("created_by") == "serving-bench":
            w.serving_endpoints.delete(name=ep["name"])
            deleted.append(ep["name"])
print(f"Deleted endpoints: {deleted or 'none'}")

# COMMAND ----------

# DBTITLE 1,Readout
probes = [r | {"latency_ms": r["latency_ms"] if r["success"] else None}
          for r in idle_rows if r["phase"] == "idle_probe"]
rows = readout(summary_rows, probes)
table = readout_markdown(rows)
displayHTML(f"<pre>{table}</pre>")
print(table)
meta = {"run_id": RUN_ID, "model": MODEL["hf_name"], "task": TASK, "input_tokens": PROMPT_TOKENS, "ai_env": MODEL.get("ai_env"),
        "date": STARTED_AT.date().isoformat(), "workspace": WORKSPACE_URL, "load_test": LT, "idle_test": IDLE,
        "endpoints": [ep | {"label": label} for label, ep in ENDPOINTS.items()]}
dbutils.notebook.exit(json.dumps({**meta, "readout": rows, "readout_markdown": table,
                                  "readout_document": readout_document(meta, rows), "deleted_endpoints": deleted}))
