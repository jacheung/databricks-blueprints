# Parking Lot

Items deferred for future work. Reference these before starting new sessions on this project.

---

## 1 — Temporal Train/Test Split

**What:** Replace the current random 80/20 split with a temporal split — train on the earliest 80% of each user's orders, test on the most recent 20%. More realistic evaluation that simulates predicting what a user will buy next.

**Why deferred:** Changes the data distribution for notebooks 01 and 02 — their Hit@K baselines will shift. Worth doing as a deliberate re-baselining exercise, not bundled with other changes.

**Entry point:** `00_data_preparation.ipynb` Cell 5 — replace `randomSplit([0.8, 0.2], seed=42)` with a window-function-based temporal split using `order_timestamp` (now available on the cleaned dataset).

---

## 2 — Scale Synthetic Data + Widen Two-Tower Model

**What:** Two changes to stress the A10 GPUs and make DDP meaningful:
1. Increase synthetic data in `00_data_preparation.ipynb`: `num_users = 50_000`, `num_orders = 50_000` → ~150K interaction pairs, ~150 steps/epoch
2. Widen the model in `03_two_tower.ipynb` Cell 3: `embed_dim = 256`, `hidden_dim = 512`, `batch_size = 8192`

**Why deferred:** Current dataset (2K users, 15K orders → ~45K pairs, ~44 steps/epoch) is sufficient for an end-to-end run. GPU saturation is not a priority until the pipeline is validated.

**Why it matters:** At `embed_dim=64`, GEMM inner dimensions are too thin for A10 tensor cores (~15-25% utilization regardless of data volume). Widening to 256+ and raising batch_size to 8192 is the right lever alongside more data.

**Entry points:** `00_data_preparation.ipynb` Cell 2 (`num_users`, `num_orders`); `03_two_tower.ipynb` Cell 3 (`embed_dim`, `hidden_dim`, `batch_size`).

---

## 3 — Add Compute Sizing Narrative to Notebook 03

**What:** Bake the model size and cluster headroom analysis into a markdown cell in `03_two_tower.ipynb` — likely as a callout at the top of Section 2 (Ray Cluster Setup), before the `setup_ray_cluster` call.

**The artifact to use:**

| Component | Params | Memory (float32) |
|---|---|---|
| User embedding (2K users × 64) | 128,000 | 512 KB |
| Item embedding (18 items × 64) | 1,152 | 5 KB |
| User MLP (71→128→64) | 17,472 | 70 KB |
| Item MLP (68→128→64) | 17,088 | 68 KB |
| **Total** | **~164K** | **~656 KB** |

Model + gradients + Adam optimizer states ≈ **3 MB per worker**. Dataset shard (~22K rows) ≈ **1.5 MB per worker**. Total per Ray worker: ~15-20 MB.

Key point to make in the narrative: this model is memory-trivial on any modern cluster — the sizing constraint is GPU compute throughput (GEMM efficiency), not RAM. For CPU-only clusters, drop `num_gpus_worker_node` from `setup_ray_cluster` and set `use_gpu=False` in `ScalingConfig`.

**Entry point:** `03_two_tower.ipynb` Cell 7 (Section 2 markdown) — expand the cluster config callout with this table and narrative.

---

## 4 — Tower Architecture Progression: Simple → Complex

**What:** Expand the two-tower model section into an explicit instructional arc that walks users from the current minimal architecture to a production-grade design, paired with a large dataset and multi-GPU training.

**Progression to document:**

| Stage | Architecture | When to use |
|---|---|---|
| **1 — Current** | `[ID emb ‖ features] → 2-layer MLP → L2-norm` | Small dataset (<100K users), few features, fast iteration |
| **2 — Separate branches** | ID emb and feature vector processed by separate sub-MLPs before merge | Feature set is heterogeneous; don't want dense feature gradients drowning embedding signal |
| **3 — Deeper + residual** | 3-4 layer MLP with residual connections, BatchNorm | Larger dataset; training instability or underfitting observed |
| **4 — Gating** | Learned sigmoid gate over feature vector before concat | Many features of uneven quality; let model suppress noise |
| **5 — Asymmetric + dropout** | Deeper user tower than item tower, dropout on both | Millions of users; ID embedding starts memorising without regularisation |

**Why deferred:** Requires the large synthetic dataset (item 2: 50K users, 150K orders) and multi-GPU cluster to make the deeper architectures meaningful. On 2K users/18 items a 4-layer tower with gating just overfits — the lesson only lands when the model actually needs the capacity.

**Entry point:** `03_two_tower.ipynb` Cell 14 (model definition) — replace the symmetric 2-layer towers with a configurable `tower_depth` and `use_gating` flag in the config cell, then add a markdown cell before the model definition that narrates the progression.
