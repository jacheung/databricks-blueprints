# Spark vs Ray for per-group Prophet forecasting

## Question

For training and forecasting one Prophet model per group, is Spark `applyInPandas` or Ray Data `map_groups` faster on the same number of cores?

## Takeaway

**No engine wins. Use Spark by default.** On matched cores, each 10k cell is decided by 6–8%, and each time most of the gap is the cluster-to-cluster difference in raw Prophet fit speed:

| Groups | Sizes | Cores | Partitions | Spark wall (s) | Ray wall (s) | Ray speedup | Sum of fits ÷ cores, Spark / Ray (s) | Efficiency, Spark / Ray |
|---:|---|---:|---:|---:|---:|---:|---:|---:|
| 1,000 | uniform | 64 | 256 | 415.8 | 407.2 | 1.02× | 330.5 / 328.2 | 79.5% / 80.6% |
| 10,000 | uniform | 64 | 256 | 3,901.7 | 3,667.5 | 1.06× | 3,405 / 3,295 | 87.3% / 89.8% |
| 10,000 | skewed | 64 | 256 | 3,637.2 | 3,955.6 | 0.92× | 3,254 / 3,432 | 89.5% / 86.8% |
| 10,000 | skewed | 128 | 10,000 (1 per group) | 2,167.2 | 2,043.4 | 1.06× | 1,871 / 1,798 | 86.3% / 88.0% |

_Efficiency = (sum of per-group fit times ÷ cores) ÷ wall time, i.e. how much of the cores' time went into Prophet fits._

- **The work is all Prophet fits.** Each engine keeps its 64 cores 87–90% busy at 10k groups. In both 10k cells, the faster arm was the one whose cluster fit Prophet models faster overall: 3% fewer fit-seconds in the uniform cell (Ray's cluster), 5% fewer in the skewed cell (Spark's cluster). The two arms run on separate clusters, so that's node-to-node variation, not the engine. What's left for scheduling is ±3% of efficiency, and it goes in different directions in the two cells.
- **One partition per group (last row) doesn't change the verdict.** This cell tests Ray's expected advantage: with one group per task, a large group can't hold up the others packed with it. Ray wins by 6%, but about 4 points of that is again fit speed (Spark's cluster spent 4% more fit-seconds); Ray's scheduling edge is 1.7 points of efficiency. Efficiency didn't rise for either engine compared with 256 partitions, though this cell also doubled the cores (64 → 128, to shorten the run), which halves the groups per core and makes the last tasks weigh more. The two effects aren't separated.
- **What limits both is the largest fits, in hash order.** The biggest group fits in 354 s, about 17% of a ~2,100 s run, and neither engine starts big groups first. Finer partitions can't split one group. Starting groups largest-first (sort by row count, then partition in that order) should shorten the tail; that's untested.
- **Skew didn't favor Ray at 256 partitions, despite our prediction.** Group sizes ran from 616 to 989,781 rows (median 30k) and fits from seconds up to 354 s. But with about 40 groups per partition, the large groups average out, and the slowest single fit is under 10% of the wall time. Ray's per-task scheduling only pays off when one group's fit is a large share of the job.
- **Spark gets the same answer on less infrastructure.** For the same 64 cores, the Ray arm needs 2 more workers (7 nodes vs 5) to keep Spark free for writing to Delta. It also needs a classic cluster (no serverless), a SQL warehouse for `read_databricks_tables` (alpha), and the Parquet handoff back to Spark. Reach for Ray when the per-group work itself needs it: multi-core or GPU tasks, actor state, or a pipeline that's already on Ray.
- **Scale helps both engines equally.** Efficiency rises from about 80% at 1k groups to about 87–90% at 10k, as the slow last tasks become a smaller share of the run.

Conditions and limitations:

- One run per cell. The 3–5% cluster-to-cluster difference in fit speed sets the noise floor; repeat runs, or running both arms on the same nodes, would tighten it.
- Only **train + forecast** is timed. Each arm loads its input first (Spark: read + `cache()`; Ray: `read_databricks_tables(...).materialize()`), and the Delta write happens after the clock stops. Both timed sections include the group-by shuffle.
- Synthetic hourly series from `00` (50k rows per group, 2 unused regressors), forecast horizon 14, `Prophet(daily_seasonality=True)`.
- Fits on these nodes are about 4.7× slower than one fit on a laptop (20.6 s vs 4.4 s). Running 16 concurrent Stan fits per node is a likely cause, but that wasn't isolated. It affects both engines equally.

## How the comparison is matched

| | Spark arm (`01`) | Ray arm (`02`) |
|---|---|---|
| Cluster | 4 × `m5.4xlarge` workers (8 for the 128-core cell), 18 LTS ML | 6 × `m5.4xlarge` workers (10 for the 128-core cell), 18 LTS ML |
| Compute used | 64 (or 128) task slots | 64 (or 128) Ray CPUs: 4 (or 8) workers × 16, head on the driver with 0 CPUs |
| Left over | — | 2 Spark workers, kept for writing results back to Delta |
| Parallelism | `repartition(p, "group_name")`, AQE off; p = 256, or 1 per group (default) | `groupby("group_name", num_partitions=p)`, `num_cpus=1` per task |
| Read (untimed) | `spark.read.table` | `ray.data.read_databricks_tables` through a dedicated serverless SQL warehouse |
| Write (untimed) | `saveAsTable` | Parquet on a UC Volume, then Spark → Delta |

The Ray setup follows [`ray-on-databricks-rct/parallelize-prophet-forecasting`](https://github.com/jacheung/ray-on-databricks-rct/tree/main/parallelize-prophet-forecasting) (`setup_ray_cluster`, Volume temp dir, `map_groups`). Two changes for Ray 2.60: call `ray.init()` after `setup_ray_cluster`, and write through Parquet, since `Dataset.write_databricks_table` is no longer in Ray.

Read tasks run on Ray workers, which have no notebook auth context. So the notebook resolves its own short-lived credential on the driver (`WorkspaceClient().config.authenticate()`) and passes it to Ray's `StaticCredentialProvider`; no personal access token is needed. The warehouse returns `ds` with a UTC timezone, which Prophet rejects, so `forecasting._fit` strips it.

## How to run

Prerequisites: a workspace with classic compute (Ray-on-Spark needs a dedicated cluster; serverless-only workspaces won't work), `m5.4xlarge` quota for 10 + 8 workers (the defaults; both arms can run at once), and `CREATE TABLE` / `CREATE VOLUME` on the schema. The bundle targets `e2-demo-fe` with `main.jon_cheung`; change the `catalog` / `schema` variables in `databricks.yml` for your own.

```bash
databricks bundle deploy -t e2-demo-fe

P="n_groups=1000,skew=false"
databricks bundle run generate_data -t e2-demo-fe --params "$P"   # timeseries_1000_groups
databricks bundle run spark_arm     -t e2-demo-fe --params "$P"   # appends a row to benchmark_results
databricks bundle run ray_arm       -t e2-demo-fe --params "$P"
databricks bundle run readout       -t e2-demo-fe                 # markdown table, latest run per cell
```

The two arms use separate job clusters, so they can run in parallel. Set `skew=true` for log-normal group sizes with the same mean. Each job cluster terminates when its run ends; the warehouse auto-stops after 10 minutes. `databricks bundle destroy -t e2-demo-fe` removes the jobs and the warehouse. The tables (`timeseries_*`, `forecasts_*`, `benchmark_results`) and the `ray_*` volumes stay in the schema until you drop them.

| File | Purpose |
|---|---|
| `00_generate_timeseries_data.py` | Synthetic series; `n_groups`, `rows_per_group`, `skew` |
| `01_spark_arm.py` / `02_ray_arm.py` | The two timed arms |
| `03_readout.py` | Readout table from `benchmark_results` |
| `lib/forecasting.py` | Shared train + forecast function (a copy of the solution's) |
| `lib/bench.py` | Result row and readout table; tested in `tests/` |
