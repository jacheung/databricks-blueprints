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
# MAGIC `synthetic_delta_inline_{size}`, `synthetic_delta_inline_file_{size}`).
# MAGIC
# MAGIC | # | format | size |
# MAGIC |---|--------|------|
# MAGIC | 1 | lance               | 10k  |
# MAGIC | 2 | lance               | 100k |
# MAGIC | 3 | lance               | 1m   |
# MAGIC | 4 | delta_inline_binary | 10k  |
# MAGIC | 5 | delta_inline_binary | 100k |
# MAGIC | 6 | delta_inline_binary | 1m   |
# MAGIC | 7 | delta_inline_file   | 10k  |
# MAGIC | 8 | delta_inline_file   | 100k |
# MAGIC | 9 | delta_inline_file   | 1m   |
# MAGIC | 10 | delta_pathref      | 10k  |
# MAGIC | 11 | delta_pathref      | 100k |
# MAGIC
# MAGIC path-ref is intentionally not carried to 1M (the per-image PUT storm is the clear loser by 100k).

# COMMAND ----------

# DBTITLE 1,Define runs and cluster spec
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.jobs import SubmitTask, NotebookTask, Source
from databricks.sdk.service.compute import (
    ClusterSpec, DataSecurityMode, AwsAttributes,
    AwsAvailability, EbsVolumeType, Library,
)
import time

w = WorkspaceClient()

# ── Notebook path ──
BASE = "/Workspace/Users/jon.cheung@databricks.com/databricks-bookshelf/multimodal-lance/benchmark"
NB_01 = f"{BASE}/01_create_datasets"

# ── Single cluster spec: 8 workers × m4.4xlarge (16 CPUs), Single-User UC ──
# Latest DBR with a matching lance-spark-bundle build, per docs.databricks.com/aws/en/release-notes/runtime/
# and Maven Central (repo1.maven.org/maven2/org/lance/lance-spark-bundle-*/maven-metadata.xml):
#   DBR 16.4 LTS -> Spark 3.5.2 / Scala 2.12 -> lance-spark-bundle-3.5_2.12
#   DBR 17.3 LTS -> Spark 4.0.0 / Scala 2.13 -> lance-spark-bundle-4.0_2.13
#   DBR 18 LTS   -> Spark 4.1.0 / Scala 2.13 -> lance-spark-bundle-4.1_2.13   <- pinned below (latest LTS)
#   DBR 19       -> Spark 4.2.0 / Scala 2.13 -> lance-spark-bundle-4.2_2.13   (newest overall, not LTS yet)
# All lance-spark-bundle-* artifacts above are published at the same latest version, 0.7.1 (as of
# 2026-07-30) — 0.0.6 (previously pinned) was never released and would fail dependency resolution.
# Picked 18 LTS over 19 for a benchmark: LTS gets a 3-year support window vs. 19's rolling channel.
# The JAR must live in a Volume and be on the UC allowlist (Maven coords aren't supported).
# Run cell 3 once to download and allowlist it.
LANCE_JAR_PATH = "/Volumes/main/jon_cheung/lance_benchmark/jars/lance-spark-bundle-4.1_2.13-0.7.1.jar"

# ── Single cluster spec (shared by all runs) ──
# No spark_conf catalog settings needed: 01_create_datasets uses the DataSource API
# (format("lance")) which only needs the JAR on the classpath — no UC catalog required.
# UC on SINGLE_USER clusters intercepts catalog lookups, making spark.sql.catalog.* invisible.
CLUSTER_SPEC = ClusterSpec(
    spark_version="18.x-scala2.13",
    node_type_id="m4.4xlarge",
    num_workers=8,
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

# No cluster libraries needed: lance writes use pylance (pip-installed in 01_create_datasets),
# not the Spark DataSource connector. Both the Spark catalog and DataSource connector fail on
# UC SINGLE_USER clusters, so we bypass them entirely.

# ── Common base_parameters shared across all runs ──
COMMON_PARAMS = {
    "catalog": "main",
    "schema": "jon_cheung",   # UC schema names can't contain "." (was failing CreateVolume with InvalidParameterValue)
    "volume": "lance_benchmark",
    "seed": "42",
    "embedding_dim": "512",
}

# ── Run grid: (format × size) — one 01_create_datasets invocation each ──
# Each run specifies which cluster spec to use (lance needs the init script).
RUNS = [
    # All 4 formats × 10k
    # {"name": "lance_10k",               "params": {"format": "lance",               "size": "10k"},  "cluster": "lance"},
    # {"name": "delta_inline_binary_10k",  "params": {"format": "delta_inline_binary",  "size": "10k"},  "cluster": "delta"},
    {"name": "delta_inline_file_10k",    "params": {"format": "delta_inline_file",    "size": "10k"},  "cluster": "delta"},
    # {"name": "delta_pathref_10k",        "params": {"format": "delta_pathref",        "size": "10k"},  "cluster": "delta"},
    # All 4 formats × 100k
    # {"name": "lance_100k",              "params": {"format": "lance",               "size": "100k"}, "cluster": "lance"},
    # {"name": "delta_inline_binary_100k", "params": {"format": "delta_inline_binary",  "size": "100k"}, "cluster": "delta"},
    # {"name": "delta_inline_file_100k",   "params": {"format": "delta_inline_file",    "size": "100k"}, "cluster": "delta"},
    # {"name": "delta_pathref_100k",       "params": {"format": "delta_pathref",        "size": "100k"}, "cluster": "delta"},
    # delta_inline (both) + lance × 1M
    # {"name": "lance_1m",                "params": {"format": "lance",               "size": "1m"},   "cluster": "lance"},
    # {"name": "delta_inline_binary_1m",   "params": {"format": "delta_inline_binary",  "size": "1m"},   "cluster": "delta"},
    # {"name": "delta_inline_file_1m",     "params": {"format": "delta_inline_file",    "size": "1m"},   "cluster": "delta"},
]

print(f"Submitting {len(RUNS)} runs, each on its own fresh 8-worker m4.4xlarge cluster (DBR 18 LTS)")

# COMMAND ----------

# DBTITLE 1,Fix: Add Lance JAR to UC allowlist (run once, requires metastore admin)
# ─── ONE-TIME FIX: Download Lance JAR to a Volume and allowlist it ───
# The UC JAR allowlist only accepts /Volumes/ paths (or s3/abfss/gs URIs),
# NOT Maven coordinates. So we must:
#   1. Download the JAR into a Volume
#   2. Add that Volume path to the allowlist
# Requires metastore admin for step 2.

import urllib.request, os
from databricks.sdk import WorkspaceClient
from databricks.sdk.service.catalog import ArtifactType, ArtifactMatcher, MatchType

w = WorkspaceClient()

# ── Step 1: Download the lance-spark-bundle JAR to a UC Volume ──
JAR_NAME = "lance-spark-bundle-4.1_2.13-0.7.1.jar"
MAVEN_URL = f"https://repo1.maven.org/maven2/org/lance/lance-spark-bundle-4.1_2.13/0.7.1/{JAR_NAME}"
VOLUME_JAR_DIR = "/Volumes/main/jon_cheung/lance_benchmark/jars"
JAR_PATH = f"{VOLUME_JAR_DIR}/{JAR_NAME}"

os.makedirs(VOLUME_JAR_DIR, exist_ok=True)

if os.path.exists(JAR_PATH):
    print(f"✓ JAR already exists: {JAR_PATH}")
else:
    print(f"Downloading {JAR_NAME} from Maven Central...")
    urllib.request.urlretrieve(MAVEN_URL, JAR_PATH)
    size_mb = os.path.getsize(JAR_PATH) / (1024 * 1024)
    print(f"✓ Downloaded: {JAR_PATH} ({size_mb:.1f} MB)")

# ── Step 2: Add the Volume path to the UC JAR allowlist ──
current = w.artifact_allowlists.get(artifact_type=ArtifactType.LIBRARY_JAR)
existing = list(current.artifact_matchers) if current.artifact_matchers else []

allowlist_path = f"{VOLUME_JAR_DIR}/"
if not any("lance" in (m.artifact or "").lower() for m in existing):
    updated = existing + [
        ArtifactMatcher(
            artifact=allowlist_path,
            match_type=MatchType.PREFIX_MATCH,
        )
    ]
    w.artifact_allowlists.update(
        artifact_type=ArtifactType.LIBRARY_JAR,
        artifact_matchers=updated,
    )
    print(f"✓ Added '{allowlist_path}' to UC JAR allowlist ({len(updated)} entries total)")
else:
    print("✓ Lance Volume path already in allowlist")

print(f"\n── Now update CLUSTER_LIBRARIES to reference: {JAR_PATH}")

# COMMAND ----------

# DBTITLE 1,Submit all runs in parallel
# Submit every run — same cluster spec, lance runs get the JAR library.
submitted = []
for run_def in RUNS:
    params = {**COMMON_PARAMS, **run_def["params"]}
    result = w.jobs.submit(
        run_name=run_def["name"],
        tasks=[
            SubmitTask(
                task_key="create_dataset",
                new_cluster=CLUSTER_SPEC,
                notebook_task=NotebookTask(
                    notebook_path=NB_01,
                    source=Source.WORKSPACE,
                    base_parameters=params,
                ),
            )
        ],
    )
    submitted.append({"name": run_def["name"], "run_id": result.run_id})
    print(f"  Submitted: {run_def['name']} ({run_def['cluster']}) → run_id={result.run_id}")

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
