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

> Measured across **10k and 100k** tiers with streaming Ray Data → Ray Train (block-order + local-buffer shuffle) DDP. The 10k and 100k tiers run on **2 × A10 (16 CPU each)**; the 1M tier runs on **4 × A10 (32 CPU each)**. Headline metrics are **samples/sec** and **time-to-first-batch (TTFB)** — both end-to-end wall-clock, immune to async-CUDA timing artifacts. Larger tiers (1M/10M) still to come.

### Read (training throughput) — inline Delta ties Lance; path-ref is the loser

Two points, both a direct consequence of *streaming* the data (the idiomatic Ray Data pattern — sequential block scan, then block-order + local-buffer shuffle, **not** per-batch random row access):

- **No random-access penalty, so inline Delta ≈ Lance — and it holds across scale.** Streaming never does storage-layer random reads, so the layout that would separate the formats (Lance's O(1) fragment addressing vs Parquet row groups) never gets exercised. Inline Delta lands at **0.98× Lance at 10k and 1.03× at 100k** — a dead tie both tiers, i.e. not a small-scale fluke. For the training *read* path, inline Delta is a legitimate option.
- **TTFB is the big tell for path-ref, and the gap widens with scale.** Path-ref Delta pays one Volumes GET **per image, per batch**; that tax compounds as the dataset grows. Throughput slips from **0.69× → 0.40× Lance (10k → 100k)** and TTFB blows out from **~2× → ~6× slower** (10.3s → 96.8s for Lance vs 21.3s → **593s** for path-ref) — nearly 10 minutes of idle GPUs before the first step at 100k. Lance's one structural read edge that streaming *doesn't* erase: it reads object storage directly and skips the shared **SQL warehouse** both Delta readers route through.

| | Lance | Delta (inline) | Delta (path-ref) |
|--|-------|----------------|------------------|
| Throughput vs Lance (10k → 100k) | baseline | **0.98× → 1.03×** (tie) | **0.69× → 0.40×** (widening) |
| TTFB vs Lance (10k → 100k) | 10.3s → 96.8s | ≈ Lance | **~2× → ~6× slower** (593s @ 100k) |
| Why | direct object-store read, no warehouse | bulk SQL scan, bytes shipped inline | one Volumes GET **per image, per batch** |

### Write & update — Lance's decisive, unmanufactured advantage

Two points, this time favoring Lance so strongly the trend line is the story:

- **Ingest scales flat; the Delta paths don't.** Lance writes via distributed fragment commits: **4.5s / 16 files → 6.4s / 64 files (10k → 100k)** — essentially flat, ~one PUT per fragment. Path-ref Delta issues **~one object-store PUT per image** — a literal PUT storm of **10,001 → 100,001 files** and **81s → 554s** wall-clock. Inline Delta avoids the file storm (16 → 64 files) but funnels **every image byte through Spark**, which is what caps it — fine at 100k (38s), projected to OOM at 1M+.
- **Feature backfill: Lance rewrites almost nothing; inline Delta rewrites the whole table.** Adding a derived column with Lance `add_columns` writes only the new column — **0.1 MB → 0.5 MB (10k → 100k)**, flat regardless of image payload. Inline Delta's `ALTER` drags all the image bytes through a full row-group rewrite: **688 MB → 6,867 MB**, scaling linearly with the dataset (→ ~69 GB projected at 1M). Path-ref's backfill is cheap in bytes (metadata only, ~190 MB) but only because the images never lived in the table — the cost you deferred is paid back on every read.

| | Lance | Delta (inline) | Delta (path-ref) |
|--|-------|----------------|------------------|
| Write (10k → 100k) | **4.5s → 6.4s**, 16 → 64 files | 10s → 38s, funnels via Spark | 81s → 554s, **10k → 100k files** |
| Object-store PUTs | ~one per fragment (dozens) | one per Parquet file | **~one per image** |
| Add-column rewrite (10k → 100k) | **0.1 → 0.5 MB** (new col only) | 688 → **6,867 MB** (drags image bytes) | ~19 → 190 MB (metadata only) |
| Scale ceiling | distributed fragments | **Spark funnel → OOM at 1M+**; ~2.1GB per-cell Parquet cap | file-count / PUT storm |
| Dataset versioning | first-class fragment versions | Delta time-travel | Delta time-travel |

**Takeaway:** for the streaming training *read* path, inline Delta is a genuine tie with Lance — streaming doesn't reward the layout, and we don't pretend otherwise. Lance's real, durable value is on the **write and data-management side**: flat-scaling ingest that inline Delta's Spark funnel can't match, near-zero-rewrite feature backfill (0.5 MB vs 6.9 GB at 100k), and large-blob support past Parquet's per-cell ceiling. Path-ref Delta is the read-side loser on every tier, and the gap only widens with scale.

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
