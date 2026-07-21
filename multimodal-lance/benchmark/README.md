# Ray multimodal benchmark: Delta vs Lance

Measure how Ray Data + Ray Train perform when the underlying storage format is **Delta** (Parquet-backed, the Databricks-native default) vs **Lance**, across increasing scale (10k → 100k → 1M → 10M rows) of synthetic multimodal data (image + caption + embedding + metadata).

The comparison reflects the *real Databricks patterns* for each format, not a synthetic apples-to-apples:

- **Lance** stores image bytes **inline** in its blob layout.
- **Delta** stores an **`image_path`** reference; the JPEG files live in a UC Volume. This is how images are actually stored in Delta — a Delta row holding 100KB+ blobs inline is a config nobody runs at scale.

That difference *is* the benchmark. The Lance branch reads bytes directly; the Delta branch pays a per-image object-storage GET hop on every batch (README failure-mode #1 in the parent blueprint). Everything else — RNG seed, cluster config, the in-memory dataset the two branches are written from — is held constant.

> **Delta is Parquet underneath**, so the row-group mechanics still apply. "Delta" here means the UC-registered table + its Parquet files, read via `read_databricks_tables`.

![Benchmark pipeline: setup → generate synthetic data → branch to Delta/Lance write, preprocess, train → compare metrics](pipeline.png)

## Notebooks

| Notebook | Description |
|----------|-------------|
| `01_create_benchmark_datasets.ipynb` | Generate the synthetic dataset once, write it to both Delta (path refs + JPEG files) and Lance (inline) |
| `02_training_benchmark.ipynb` | Feed each format through Ray Train, capture data-loading and training throughput |

## Compute & connectors

- **Data generation** (`01`) — Databricks Classic Compute, **8 worker nodes × 16 CPUs**.
- **Training** (`02`) — **4 × A10** GPUs (Ray Train DDP); scale to 8 for the 1M/10M tiers.
- **SQL Warehouse** — both `ray.data.write_databricks_table` and `ray.data.read_databricks_tables` route through a running SQL Warehouse. The notebooks provision-or-reuse a serverless warehouse with a short auto-stop, and accept a `warehouse_id` widget to pin an existing one.

## Pipeline overview

### 1. Setup

Fix the RNG seed and the Ray cluster config (node count, CPU/GPU per node, object store memory) once, and hold both constant across the Delta and Lance runs. This isolates the storage format as the variable under test.

### 2. Generate synthetic data

Produce the canonical dataset **once** as an in-memory Ray Dataset (`ray.data.range(N).map_batches(generate_fn)`), using the fixed seed. Both format branches write from these same in-memory blocks — never regenerate per format, or timing differences get contaminated by generation noise.

Schema:

| Column | Type | Notes |
|--------|------|-------|
| `id` | int | |
| `image` | binary | Procedurally generated JPEG bytes, ~30–300KB |
| `caption` | string | Templated variable-length text |
| `embedding` | list\<float32\> | e.g. 512-dim, mimics a CLIP embedding |
| `category` | string | Low-cardinality categorical |
| metadata | numeric | A few numeric columns |

### 3. Write — Delta / Lance

Branch the in-memory dataset:

- **Lance** — `ds.write_lance(path)`: each Ray write task emits a fragment; a single driver-side commit merges fragment metadata into a new dataset version. Image bytes stored inline.
- **Delta** — write the JPEG bytes out as files in a UC Volume, then `ds.write_databricks_table(...)` a metadata table whose `image_path` column references those files. This is the native path-reference pattern.

**Metrics to log:**

- Write throughput — rows/sec, MB/s (raw and on-disk/compressed bytes)
- On-disk size and compression ratio
- File/fragment count and average size
- Ray write-task count and achieved concurrency
- Peak worker memory during write
- Object-store PUT count if writing to S3/GCS (small-file cost)
- Retry/error counts (Lance's writer has built-in retry-with-backoff)

### 4. Preprocess — Delta / Lance

**Decision: keep decode/resize inline, not as a materialized ETL step.** Once images are decoded to fixed-size arrays, both formats are storing uniform tensors and you lose the variable-size-blob problem that's the actual reason to compare them. Decode/resize is fused into the read path (`read_databricks_tables` + per-image file read / `read_lance` → `.map_batches(transform)` → straight into training) — this also preserves per-epoch random augmentation, which a cached copy would freeze.

**Add a separate, real ETL benchmark:** compute a derived column once via a Ray UDF and backfill it into the existing dataset. This is where Lance has a structural advantage — its data-evolution / `add_columns` support backfills a new column without rewriting existing data, where Delta's metadata table needs an `ALTER TABLE ADD COLUMN` + full backfill rewrite. Measure wall-clock and total bytes rewritten for each format on this operation.

**Metrics for the inline preprocessing step (both branches):**

- Decode/resize throughput (rows/sec) under full-column read vs projected/column-selective read (image + id only, skipping embedding/text)
- For Delta: the extra per-image Volumes GET hop that Lance's inline layout avoids
- CPU utilization on the transform actors

### 5. Train — Delta / Lance

Feed the (branch-specific) dataset through Ray Train via `iter_torch_batches`, with a shuffled/random-access read pattern per epoch — this is the pattern most likely to separate the two formats; a sequential full scan will look similar for both.

Run twice per branch: once with a no-op dummy model step (isolates pure data-loading throughput), once with a real small model (attributes the bottleneck between I/O and compute).

**Metrics to capture:**

- Samples/sec, both dummy-model and real-model runs
- GPU utilization % (the tell for data-starvation)
- Time-to-first-batch
- Per-batch latency distribution — p50/p95/p99, not just mean
- Epoch-to-epoch throughput variance under shuffle
- CPU utilization on preprocessing actors
- Ray object-store spill events / disk IOPS during shuffled reads
- Actor-pool utilization (saturated vs waiting on IO)

### 6. Compare metrics

Aggregate all of the above per format, per scale tier (10k/100k/1M/10M), and look specifically at where the two formats diverge — expected to be the per-image GET hop, projected-column reads, and shuffled random-access throughput, rather than raw sequential scan speed.

## Scale tiers

| Tier | Purpose |
|------|---------|
| **10k** | Correctness pass — does the pipeline run end to end, do reads round-trip identical rows |
| **100k** | Tuning pass — actor pool sizing, batch sizing, CPU- vs IO-bound diagnosis |
| **1M** | First tier with real object-store spill and distributed shuffle; row-group pruning (Delta) vs fragment-based random reads (Lance), and the per-image GET hop, should start to show a gap |
| **10M** | Full stress test (~1–3TB at 100–300KB/row); multi-node scaling, and the tier where Lance's incremental-append/versioning advantage is most visible if you simulate incremental ingestion rather than one big write |
