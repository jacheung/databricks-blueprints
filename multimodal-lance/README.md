# Multimodal Image ML Training with Lance on Databricks

<p>
  <img src="https://img.shields.io/badge/Databricks-FF3621?style=flat-square&logo=databricks&logoColor=white" alt="Databricks"/>
  <img src="https://img.shields.io/badge/Apache%20Spark-E25A1C?style=flat-square&logo=apachespark&logoColor=white" alt="Apache Spark"/>
  <img src="https://img.shields.io/badge/Ray-028CF0?style=flat-square&logo=ray&logoColor=white" alt="Ray"/>
  <img src="https://img.shields.io/badge/Lance-6B4FBB?style=flat-square&logoColor=white" alt="Lance"/>
</p>

A blueprint demonstrating how the [Lance](https://lancedb.github.io/lance/) columnar format optimizes image-based ML training on Databricks — specifically addressing where Delta/Parquet breaks down for multimodal workloads.

The foundational image ML problem types — **classification**, **object detection**, and **segmentation** — all share the same core training bottleneck on Databricks: random-access reads of large binary payloads (raw image bytes) from a Parquet-backed store are fundamentally mismatched with how ML training DataLoaders work.

---

## Key highlights

Three ways to store images for training on Databricks, compared. **Delta (path-ref)** = the standard pattern (JPEG files in a Volume + an `image_path` column); **Delta (inline)** = bytes in a `binary` column; **Lance** = bytes inline in a blob-isolated fragment layout.

> Measured across **10k, 100k, and 1M** tiers with streaming Ray Data → Ray Train (block-order + local-buffer shuffle) DDP. Training runs on A10 GPUs (2 × A10 for 10k/100k, 4 × A10 for 1M). **Write and backfill are node-matched**: a 14-worker cluster split 7 Ray (generate/Lance-write) + 7 Spark (Delta-write), so Delta-vs-Lance write times are compute-for-compute, not skewed by node count. Delta reads route through a tier-sized SQL warehouse (Small at 10k/100k, Large at 1M). Headline metrics are **samples/sec** and **time-to-first-batch (TTFB)** — both end-to-end wall-clock, immune to async-CUDA timing artifacts.

### Read (training throughput) — inline Delta ties Lance; path-ref is the loser

Two points, both a direct consequence of *streaming* the data (the idiomatic Ray Data pattern — sequential block scan, then block-order + local-buffer shuffle, **not** per-batch random row access):

- **No random-access penalty, so inline Delta ≈ Lance — and it holds across every tier.** Streaming never does storage-layer random reads, so the layout that would separate the formats (Lance's O(1) fragment addressing vs Parquet row groups) never gets exercised. Inline Delta lands at **0.98× / 1.03× / 0.98× Lance (10k / 100k / 1M)** — a dead tie at all three tiers, not a small-scale fluke. If anything inline Delta is a hair *faster* to first batch at scale (TTFB 88.4s vs 96.8s at 100k; 208.7s vs 248.9s at 1M) — Lance carries no read-startup edge here. For the training *read* path, inline Delta is a legitimate option.
- **TTFB is the big tell for path-ref, and the gap widens with scale.** Path-ref Delta pays one Volumes GET **per image, per batch**; that tax compounds as the dataset grows. Throughput slips from **0.69× → 0.40× Lance (10k → 100k)** and TTFB blows out from ~2× to ~6× slower (10.3s → 96.8s for Lance vs 21.3s → **593s** for path-ref) — nearly 10 minutes of idle GPUs before the first step at 100k. (Path-ref is so clearly the read-side loser by 100k that we didn't carry it to 1M.) Lance's one structural read edge that streaming *doesn't* erase: it reads object storage directly and skips the **SQL warehouse** both Delta readers route through.

| Read (streaming train) | Lance | Delta (inline) | Delta (path-ref) |
|--|-------|----------------|------------------|
| Throughput vs Lance (10k → 100k → 1M) | baseline | **0.98× → 1.03× → 0.98×** (tie) | **0.69× → 0.40×** (widening) |
| TTFB (10k → 100k → 1M) | 10.3s → 96.8s → 248.9s | 10.7s → 88.4s → 208.7s (≈ Lance, edges ahead at scale) | 21.3s → **593s** (path-ref not run at 1M) |
| Why | direct object-store read, no warehouse | bulk SQL scan, bytes shipped inline | one Volumes GET **per image, per batch** |

### Write & update — Lance's decisive, unmanufactured advantage

Three points, all favoring Lance so strongly the trend line is the story — and now on a **node-matched** cluster (7 Ray vs 7 Spark), so this is compute-for-compute, not a node-count artifact:

- **Ingest scales flat; the Delta paths don't.** Lance writes via distributed fragment commits: **3.4s → 5.1s → 26.6s (10k → 100k → 1M)**, ~one PUT per fragment (16 → 64 → 600 files). Path-ref Delta issues **~one object-store PUT per image** — a literal PUT storm of **10,001 → 100,001 files** and **48.7s → 266.8s** wall-clock (so clearly losing we didn't carry it to 1M). Inline Delta avoids the file storm (16 → 64 → 600 files) but funnels **every image byte through Spark**, which is what caps it: fine at small scale (14.9s / 13.4s at 10k / 100k) but **242.5s at 1M — 9.1× behind Lance's 26.6s**. The funnel is a *scale* effect, not a constant tax: it stays cheap until the data volume saturates Spark, then degrades sharply.
- **Feature backfill: Lance now wins on *both* bytes and wall-clock.** Backfilling a derived column with Lance's distributed `merge_columns` writes only the new column — **0.1 → 0.5 → 3.8 MB (10k → 100k → 1M)**, flat regardless of image payload — in **2.4s → 2.7s → 8.8s**. Inline Delta's `ALTER` + `UPDATE` drags all the image bytes through a full row-group rewrite: **688 MB → 6,867 MB → 68,648 MB** and **3.0s → 9.0s → 46.9s** (so Lance is **5.3× faster** at 1M *and* writes ~18,000× fewer bytes). Path-ref's backfill is cheap in bytes (metadata only, ~190 MB at 100k) but only because the images never lived in the table — the cost you deferred is paid back on every read.
- **Both Lance operations are distributed on Ray**, matching Delta's distributed Spark write/`UPDATE` — so the wall-clock wins are a fair fight, not Lance-parallel vs Delta-serial.

**Write (ingest)**

| Write (ingest) | Lance | Delta (inline) | Delta (path-ref) |
|--|-------|----------------|------------------|
| Write time (10k → 100k → 1M) | **3.4s → 5.1s → 26.6s** | 14.9s → 13.4s → **242.5s** (funnels via Spark) | 48.7s → 266.8s (not run at 1M) |
| Files written ≈ object-store PUTs (10k → 100k → 1M) | **16 → 64 → 600** (one per fragment) | 16 → 64 → 600 (one per Parquet file) | **10,001 → 100,001** (one per image + table; not run at 1M) |

**Backfill (schema evolution — add + populate a derived column)**

| Backfill | Lance | Delta (inline) | Delta (path-ref) |
|--|-------|----------------|------------------|
| Time (10k → 100k → 1M) | **2.4s → 2.7s → 8.8s** | 3.0s → 9.0s → 46.9s | 8.7s → 3.2s (metadata only, noise-dominated) |
| Bytes written (10k → 100k → 1M) | **0.1 → 0.5 → 3.8 MB** (new col only) | 688 → 6,867 → **68,648 MB** (drags image bytes) | ~19 → 190 MB (metadata only) |
| Files rewritten (10k → 100k → 1M) | one new col file per fragment (16 → 64 → 600) — data files untouched | 16 → 64 → **605** (whole row groups, image bytes and all) | 2 → 21 (metadata-only Parquet; not run at 1M) |

*Backfill = adding a **derived** column (here the L2 norm of the embedding) and populating it across all existing rows. The `ALTER TABLE ADD COLUMN` step is metadata-only and free on both formats — only **filling** the rows triggers a rewrite, so the numbers above are that fill. Delta must rewrite whole row groups (dragging the image bytes along); Lance's distributed `merge_columns` writes only the new column file per fragment and leaves the data files untouched.*

**Scaling & capabilities**

| Scaling & capabilities | Lance | Delta (inline) | Delta (path-ref) |
|--|-------|----------------|------------------|
| Scale behavior | distributed fragments, flat | **Spark funnel — 9.1× behind at 1M**; ~2.1GB per-cell Parquet cap | file-count / PUT storm |
| Max inline blob (per cell) | **No limit** — byte-offset addressed | **~2 GB** hard Parquet cap (32-bit length prefix) | n/a — bytes live in Volume files |
| Dataset versioning | first-class fragment versions | Delta time-travel | Delta time-travel |

**Takeaway:** for the streaming training *read* path, inline Delta is a genuine tie with Lance at all three tiers — streaming doesn't reward the layout, and we don't pretend otherwise (inline even edges Lance on TTFB at scale). Lance's real, durable value is on the **write and data-management side**, and node-matching only sharpened it: flat-scaling ingest (26.6s vs 242.5s = **9.1× at 1M**) that inline Delta's Spark funnel can't match once the data saturates Spark, and feature backfill that now wins on **both** bytes (3.8 MB vs 68.6 GB at 1M) **and** wall-clock (8.8s vs 46.9s = 5.3×), plus large-blob support past Parquet's per-cell ceiling. Path-ref Delta is the read-side loser on every tier, and the gap only widens with scale.

### Beyond throughput: two Lance capabilities Delta can't match

These aren't stopwatch wins — the streaming benchmark doesn't exercise either — but they're real capability gaps, not performance deltas that wash out at scale:

- **Random point-access reads.** Lance's O(1) byte-offset addressing serves scattered, by-ID row fetches directly from the training store — the pattern behind data curation, error analysis, active-learning selection, and vector/ANN retrieval co-located with the image bytes. Delta can't serve this from the same store (you'd bolt on a separate vector DB and keep it in sync). Against training wall-clock the *latency* saved is negligible; the value is the **capability** and the single versioned store, not speed.
- **Large blobs past Parquet's per-cell cap.** Parquet encodes a binary cell with a 32-bit length prefix, so a single inline value can't exceed **~2 GB** — a hard format wall that inline Delta hits on full-res video clips, whole-slide pathology images, or volumetric scans. Lance's blob layout addresses payloads by byte-offset with no per-cell ceiling. The current JPEG benchmark (~150 KB/image) never approaches this, but it's the enabling difference for the heavier-payload workloads in the Parking Lot.

---

## The standard Databricks path for ML

Delta Lake + Parquet is the right default for most ML workloads on Databricks. It provides Unity Catalog governance, SQL access, Photon-accelerated queries, time-travel, and native integration with MLflow and Feature Store. For tabular features — structured data like user events, transactions, and numerical features — Delta is the correct choice with no caveats.

---

## Where it breaks for image ML — and how Lance fixes it

CNN training needs raw pixels on every batch, so image bytes are stored inline. Parquet lays rows out in ~128MB row groups; at ~100KB/image those groups collapse from ~128K rows to ~1,280, so a random batch of 64 images reads gigabytes to retrieve a few megabytes — and because all columns in a row group are co-located, every metadata scan drags the image bytes along with it. Lance stores binary payloads in an isolated blob file with fragment-level O(1) random access: a shuffled batch read fetches exactly the rows it needs at constant cost regardless of dataset size, and a metadata scan never touches the image bytes.

> **Why the benchmark's Delta path (`benchmark/01a`) stores image *paths*, not inline bytes.** Delta can hold the JPEG bytes inline in a `binary` column — but under the shuffled, random-access sampling that training DataLoaders do, that's an anti-pattern, not a fair Delta: every random batch triggers full ~128MB row-group scans (the collapse above), so inline Delta would look *worse* than the path-reference pattern, not better. Storing an `image_path` reference to files in a UC Volume is what practitioners actually run at scale, so `01a` writes it that way — the per-image Volumes GET on each batch is the real, representative Delta cost the benchmark measures, not a strawman.

|  | Lance | Delta |
|--|-------|-------|
| Random-access batch sampling | O(1) per row, fragment-level addressing | Row-group scan — 128MB per 1,280-row group for images |
| Inline binary storage | Blob layout — isolated, byte-offset addressed | All columns co-located in row group |
| Metadata scan with binary columns | Skips blob data entirely | Must traverse binary bytes to find metadata rows |
| Dataset versioning for ML | First-class — every write is a new version | SQL time-travel, not ML-oriented |

---

## Repository organization

**Happy path — `01_create_lance_dataset.ipynb`, `02_cnn_training.ipynb`.** The idiomatic Lance + Ray workflow, end to end. `01` generates a synthetic multimodal dataset (image + caption + embedding + metadata) and writes it to a Lance table in Databricks Volumes. `02` trains a CNN classifier on it with Ray Data + Ray Train. Synthetic data keeps the focus on the workflow — no external dataset to download, understand, or license — so you can run both notebooks straight through.

**`benchmark/`.** A controlled Lance-vs-Parquet comparison across scale tiers (10k → 10M rows), holding everything but the storage format constant. Measures write, preprocess, and shuffled-training throughput to quantify where the two formats diverge. See [`benchmark/README.md`](benchmark/README.md).

---

## Caveat: Unity Catalog governance tradeoffs

Lance datasets live in UC Volumes, not UC-registered tables. For most ML practitioners this is a non-issue — training pipelines are Python, not SQL, and the DataLoader doesn't care about catalog registration. Storage governance (file-level `READ FILES` / `WRITE FILES` grants, audit logging, Catalog Explorer visibility) is fully retained at the Volume level.

Two UC features that Delta provides natively are unavailable for Lance:

- **Data lineage.** UC captures column-level lineage for registered Delta tables. Path-based reads of Lance files produce no lineage entries — reads and writes are invisible to the UC lineage graph.
- **Time travel.** Delta's `VERSION AS OF` / `TIMESTAMP AS OF` SQL syntax does not apply to Lance. Lance has its own immutable fragment versioning, but it is not SQL-queryable and carries no UC-managed retention policy.

Both can be recovered with a thin Delta manifest table:

```sql
CREATE TABLE main.ml.lance_manifest (
  dataset_name  STRING,
  volume_path   STRING,
  lance_version BIGINT,
  row_count     BIGINT,
  schema_json   STRING,
  written_at    TIMESTAMP,
  written_by    STRING,
  run_id        STRING
);
```

Each write to the Lance dataset appends one row. The manifest is a registered Delta table in UC — it appears in Catalog Explorer, participates in lineage, and is queryable via SQL. To reproduce a training run or roll back to a prior dataset state, look up the `lance_version` in the manifest and call `lance.dataset(path).checkout_version(n)`.

---

## Parking Lot

- **Format benchmark vs Mosaic MDS** — extend the `benchmark/` comparison to include Mosaic Streaming MDS alongside Lance and Delta/Parquet. MDS is the Databricks-native alternative for distributed training and is meaningfully better than Parquet for binary workloads, but lacks Lance's blob isolation — the benchmark would quantify that gap concretely.

  | | Lance | Mosaic MDS | Delta/Parquet |
  |---|---|---|---|
  | Blob isolation | Yes — dedicated blob file | No — co-located per record | No — co-located per row group |
  | Random access unit | Single row, O(1) | Within-shard index | Full row group (~128MB for images) |
  | Wasted I/O per random fetch | ~0 (byte-offset seek) | Up to shard size (~67MB) | Up to row group (~128MB) |
  | Cloud-native | Yes | Yes | Yes |
  | Databricks integration | Volumes path | Volumes path | Native UC table |

- Standardize `lance` library version across all notebooks via a shared `requirements.txt` or cluster init script.
- **Audio-visual classification** — store video frames (image binary) and audio spectrograms (binary) as two heterogeneous blob columns in a single Lance table. This is the strongest demonstration of Lance's mixed-blob layout advantage over Parquet, which has no equivalent for co-locating heterogeneous binary payloads.
- **Semantic segmentation** — store image binary and pixel-level mask binary as two blob columns per row. Row-group collapse in Parquet is doubly pronounced (both blobs contribute to row group size shrinkage).
