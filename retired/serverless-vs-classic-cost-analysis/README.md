# Serverless vs Classic Compute — DBU Cost Analysis

A single notebook for comparing the **actual billed DBU** cost of running a Databricks job on classic Job Compute vs Serverless Compute, using `system.billing.usage` as the source of truth.

Useful for anyone evaluating whether to migrate a recurring job from a classic cluster to serverless, or validating the cost impact after a migration has already happened.

---

## How it works

Rather than estimating DBU from run duration × a guessed DBU rate (which can be significantly off due to contract discounts, SKU differences, and minimum billing units), this notebook queries the billing system tables directly. It:

1. **Auto-detects the comparison windows** — finds the last N classic runs and the first N serverless runs for the job from `system.billing.usage`, using the classic-to-serverless transition as the boundary
2. **Queries exact billed DBU** for both run sets
3. **Prints a side-by-side summary table**

---

## Prerequisites

- The target job must have been run on **both** compute types — at least `RUNS` executions each
- Access to `system.billing.usage` in the workspace
- A running SQL warehouse to execute the system table queries
- `databricks-sdk` installed (`pip install databricks-sdk`)

---

## Usage

Open `dbu_analysis.ipynb` and fill in the four variables at the top of the notebook:

| Variable | Description |
|---|---|
| `PROFILE` | Databricks CLI profile from `~/.databrickscfg` |
| `JOB_ID` | Numeric ID of the job to evaluate |
| `WAREHOUSE_ID` | ID of any running SQL warehouse in the workspace |
| `RUNS` | Number of runs to compare per compute type (default: 144 = 12 hrs at 5-min cadence) |

Then **run all cells**. The notebook has two steps:

- **Step 1** shows which run windows were selected — sanity-check these before proceeding
- **Step 2** prints the DBU comparison table

### Example output

```
==================================================================
  DBU COMPARISON  —  job 262685974171222  (144 runs each)
==================================================================
                                     Classic  Serverless
  ------------------------------  ----------  ----------
  Runs                                   144         144
  SKU                             JOBS_COMPUTE  SERVERLESS
  Window start                    2026-04-19  2026-04-19
  Window end                      2026-04-19  2026-04-20
  ------------------------------  ----------  ----------
  Total billed DBU                    2.7807      1.1766
  Serverless savings                    2.36x cheaper
==================================================================
```

---

## Notes

- **Why not estimate from run duration?** Runtime × assumed DBU/hr is typically 2–4x off due to contract discount tiers, instance type mismatches, and billing granularity. System tables are the only accurate source.
- **Equal windows matter.** The notebook always compares the same number of runs on each side. If your job's workload changed significantly between the classic and serverless periods, the DBU difference will be confounded — check that avg run duration is similar across both windows.
- **Pre-deployment test runs are excluded automatically.** The serverless window starts from the first run at or after the last classic run, so any early one-off serverless test runs don't skew the comparison.
