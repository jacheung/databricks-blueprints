# Databricks notebook source
# /// script
# [tool.databricks.environment]
# environment_version = "5"
# ///
# DBTITLE 1,02 — Create Datasets in Parallel
# MAGIC %md
# MAGIC # 02 — Create Datasets in Parallel
# MAGIC
# MAGIC Submit the **(format × size)** grid as concurrent one-time runs (via `jobs.submit`), each
# MAGIC on its own fresh **8-worker** classic cluster (m4.4xlarge, 16 CPUs/node). Each run invokes
# MAGIC `01_create_datasets` for one format at one size, so no run reuses another's Spark cache.
# MAGIC Polls until all complete and reports results.
# MAGIC
# MAGIC **This is a data-creation step — run it before `03_training_benchmark`**, which reads the
# MAGIC datasets these runs produce (`synthetic_lance_{size}`, `synthetic_delta_{size}`,
# MAGIC `synthetic_delta_inline_{size}`).
# MAGIC
# MAGIC | # | format | size |
# MAGIC |---|--------|------|
# MAGIC | 1 | lance          | 10k  |
# MAGIC | 2 | lance          | 100k |
# MAGIC | 3 | lance          | 1m   |
# MAGIC | 4 | delta_inline   | 10k  |
# MAGIC | 5 | delta_inline   | 100k |
# MAGIC | 6 | delta_inline   | 1m   |
# MAGIC | 7 | delta_pathref  | 10k  |
# MAGIC | 8 | delta_pathref  | 100k |
# MAGIC
# MAGIC path-ref is intentionally not carried to 1M (the per-image PUT storm is the clear loser by 100k).

# COMMAND ----------

# DBTITLE 1,Define runs and cluster spec
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import SubmitTask, NotebookTask, Source
from databricks.sdk.service.compute import (
    ClusterSpec, DataSecurityMode, AwsAttributes,
    AwsAvailability, EbsVolumeType, Library, MavenLibrary,
)
import time

w = WorkspaceClient()

# ── Notebook path ──
BASE = "/Workspace/Users/jon.cheung@databricks.com/databricks-bookshelf/multimodal-lance/benchmark"
NB_01 = f"{BASE}/01_create_datasets"

# ── Single cluster spec: 8 workers × m4.4xlarge (16 CPUs), Single-User UC ──
# DBR 16.4 LTS (Spark 3.5 / Scala 2.12): required so the lance-spark-bundle JAR loads —
# it is NOT compatible with DBR 17.x (Spark 4 / Scala 2.13). Delta runs fine on 16.4, so
# one spec serves all three formats. The Lance JAR + catalog config below are inert for
# the Delta runs (they only kick in when format=lance), so a single spec is safe for the grid.
LANCE_JAR = "org.lance:lance-spark-bundle-3.5_2.12:0.0.6"   # pin to the version you allowlisted

CLUSTER_SPEC = ClusterSpec(
    spark_version="16.4.x-scala2.12",
    node_type_id="m4.4xlarge",
    num_workers=8,
    data_security_mode=DataSecurityMode.SINGLE_USER,
    # Lance dir-namespace catalog rooted at the Volume so the dataset lands at
    # {volume}/synthetic_lance_{size} — exactly where 03 reads it via read_lance.
    spark_conf={
        "spark.sql.catalog.lance": "org.lance.spark.LanceNamespaceSparkCatalog",
        "spark.sql.catalog.lance.impl": "dir",
        "spark.sql.catalog.lance.root": "/Volumes/main/ml_benchmark/lance_benchmark",
    },
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

# lance-spark-bundle installed as a cluster library (must also be UC-allowlisted).
CLUSTER_LIBRARIES = [Library(maven=MavenLibrary(coordinates=LANCE_JAR))]

# ── Common base_parameters shared across all runs ──
COMMON_PARAMS = {
    "catalog": "main",
    "schema": "ml_benchmark",
    "volume": "lance_benchmark",
    "seed": "42",
    "embedding_dim": "512",
}

# ── Run grid: (format × size) — one 01_create_datasets invocation each ──
RUNS = [
    {"name": "lance_10k",          "params": {"format": "lance",         "size": "10k"}},
    {"name": "lance_100k",         "params": {"format": "lance",         "size": "100k"}},
    {"name": "lance_1m",           "params": {"format": "lance",         "size": "1m"}},
    {"name": "delta_inline_10k",   "params": {"format": "delta_inline",  "size": "10k"}},
    {"name": "delta_inline_100k",  "params": {"format": "delta_inline",  "size": "100k"}},
    {"name": "delta_inline_1m",    "params": {"format": "delta_inline",  "size": "1m"}},
    {"name": "delta_pathref_10k",  "params": {"format": "delta_pathref", "size": "10k"}},
    {"name": "delta_pathref_100k", "params": {"format": "delta_pathref", "size": "100k"}},
]

print(f"Submitting {len(RUNS)} runs, each on its own fresh 8-worker m4.4xlarge cluster (DBR 16.4 LTS)")

# COMMAND ----------

# DBTITLE 1,Submit all runs in parallel
# Submit every run — each gets its own isolated cluster with the Lance JAR + catalog config.
submitted = []
for run_def in RUNS:
    params = {**COMMON_PARAMS, **run_def["params"]}
    result = w.jobs.submit(
        run_name=run_def["name"],
        tasks=[
            SubmitTask(
                task_key="create_dataset",
                new_cluster=CLUSTER_SPEC,
                libraries=CLUSTER_LIBRARIES,
                notebook_task=NotebookTask(
                    notebook_path=NB_01,
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
