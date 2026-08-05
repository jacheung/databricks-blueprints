# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,04 — Run Benchmarks in Parallel
# MAGIC %md
# MAGIC # 03 — Run Benchmarks in Parallel
# MAGIC
# MAGIC Submit **8 one-time runs** (via `jobs.submit`) concurrently, each on its own 14-worker classic cluster (m4.4xlarge, 16 CPUs/node). Polls until all complete and reports results.
# MAGIC
# MAGIC Each delta mode gets its own isolated cluster to avoid Spark caching artefacts (shared DataFrame between modes inflates inline timings).
# MAGIC
# MAGIC | # | Notebook | size | delta_write_mode |
# MAGIC |---|----------|------|------------------|
# MAGIC | 1 | 01a_delta_native | 10k | inline |
# MAGIC | 2 | 01a_delta_native | 10k | pathref |
# MAGIC | 3 | 01b_lance_native | 10k | — |
# MAGIC | 4 | 01a_delta_native | 100k | inline |
# MAGIC | 5 | 01a_delta_native | 100k | pathref |
# MAGIC | 6 | 01b_lance_native | 100k | — |
# MAGIC | 7 | 01a_delta_native | 1m | inline |
# MAGIC | 8 | 01b_lance_native | 1m | — |

# COMMAND ----------

# DBTITLE 1,Define runs and cluster spec
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import SubmitTask, NotebookTask, Source
from databricks.sdk.service.compute import (
    ClusterSpec, DataSecurityMode, AwsAttributes,
    AwsAvailability, EbsVolumeType,
)
import time

w = WorkspaceClient()

# ── Notebook paths ──
BASE = "/Workspace/Users/jon.cheung@databricks.com/databricks-bookshelf/multimodal-lance/benchmark"
NB_01A = f"{BASE}/01a_delta_native"
NB_01B = f"{BASE}/01b_lance_native"

# ── Shared cluster spec: 14 workers × m4.4xlarge (16 CPUs), single-user ──
CLUSTER_SPEC = ClusterSpec(
    spark_version="17.3.x-scala2.13",
    node_type_id="m4.4xlarge",
    num_workers=14,
    data_security_mode=DataSecurityMode.SINGLE_USER,
    aws_attributes=AwsAttributes(
        availability=AwsAvailability.SPOT_WITH_FALLBACK,
        first_on_demand=1,
        spot_bid_price_percent=100,
        zone_id="auto",
        ebs_volume_type=EbsVolumeType.GENERAL_PURPOSE_SSD,
        ebs_volume_count=1,
        ebs_volume_size=100,
    ),
)

# ── Common base_parameters shared across all runs ──
COMMON_PARAMS = {
    "catalog": "main",
    "schema": "jon_cheung",
    "volume": "lance_benchmark",
    "seed": "42",
    "embedding_dim": "512",
}

# ── 8 run definitions (each mode isolated to avoid Spark caching artefacts) ──
RUNS = [
    {"name": "01a_delta_10k_inline",  "notebook": NB_01A, "params": {"size": "10k",  "delta_write_mode": "inline"}},
    {"name": "01a_delta_10k_pathref", "notebook": NB_01A, "params": {"size": "10k",  "delta_write_mode": "pathref"}},
    {"name": "01b_lance_10k",         "notebook": NB_01B, "params": {"size": "10k"}},
    {"name": "01a_delta_100k_inline", "notebook": NB_01A, "params": {"size": "100k", "delta_write_mode": "inline"}},
    {"name": "01a_delta_100k_pathref","notebook": NB_01A, "params": {"size": "100k", "delta_write_mode": "pathref"}},
    {"name": "01b_lance_100k",        "notebook": NB_01B, "params": {"size": "100k"}},
    {"name": "01a_delta_1m_inline",   "notebook": NB_01A, "params": {"size": "1m",   "delta_write_mode": "inline"}},
    {"name": "01b_lance_1m",          "notebook": NB_01B, "params": {"size": "1m"}},
]

print(f"Submitting {len(RUNS)} runs, each on its own fresh 14-worker m4.4xlarge cluster")

# COMMAND ----------

# DBTITLE 1,Submit all 6 runs in parallel
# Submit all 8 runs — each gets its own isolated cluster
submitted = []
for run_def in RUNS:
    params = {**COMMON_PARAMS, **run_def["params"]}
    result = w.jobs.submit(
        run_name=run_def["name"],
        tasks=[
            SubmitTask(
                task_key="benchmark",
                new_cluster=CLUSTER_SPEC,
                notebook_task=NotebookTask(
                    notebook_path=run_def["notebook"],
                    source=Source.WORKSPACE,
                    base_parameters=params,
                ),
            )
        ],
    )
    submitted.append({"name": run_def["name"], "run_id": result.run_id})
    print(f"  Submitted: {run_def['name']} → run_id={result.run_id}")

print(f"\n✓ All {len(submitted)} runs submitted")

# COMMAND ----------

# DBTITLE 1,Poll until all runs complete
import pandas as pd

# Poll every 60s until all runs reach a terminal state
TERMINAL_STATES = {"TERMINATED", "SKIPPED", "INTERNAL_ERROR"}

while True:
    all_done = True
    for s in submitted:
        run = w.jobs.get_run(s["run_id"])
        s["state"] = run.state.life_cycle_state.value
        s["result"] = run.state.result_state.value if run.state.result_state else None
        if s["state"] not in TERMINAL_STATES:
            all_done = False
    
    status_df = pd.DataFrame(submitted)
    print(f"\n[{time.strftime('%H:%M:%S')}] Status:")
    print(status_df.to_string(index=False))
    
    if all_done:
        break
    time.sleep(60)

print("\n" + "=" * 60)
print("All runs complete!")
print("=" * 60)

# Final summary
for s in submitted:
    run = w.jobs.get_run(s["run_id"])
    duration_s = (run.end_time - run.start_time) / 1000 if run.end_time and run.start_time else None
    s["duration_min"] = round(duration_s / 60, 1) if duration_s else None

display(pd.DataFrame(submitted))
