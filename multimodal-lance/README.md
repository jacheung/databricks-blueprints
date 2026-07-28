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
