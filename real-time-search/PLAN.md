# Build plan and status

Top-level plan for this blueprint: the question, how the tracks work together, shared resources, contracts and
decisions. Each track keeps its own checklist: [data/PLAN.md](data/PLAN.md), [ml/PLAN.md](ml/PLAN.md),
[app/PLAN.md](app/PLAN.md). Deferred scope lives in [PARKING_LOT.md](PARKING_LOT.md).

**Question:** Does a session-aware L2 reranker improve QAC and SERP relevance enough to justify its latency, and can
Databricks serve both within real-time budgets (QAC < 40 ms, SERP < 400 ms, both server-side p95 measured in the app;
feature freshness < 1 s)? What the user waits for is timed in the browser: QAC keystroke → on screen p95 ≤ 120 ms
(the 20 ms debounce + ≤ 100 ms network, server and render).

## Start here (new session)

Read this file, then the plan for your track only.

| Track | Worktree and branch | Status |
|---|---|---|
| app | worktree `~/GitHub/databricks-blueprints-rts-app`, branch `rts-app` | QAC L1 + L2 demo: Under-the-hood panel, L2 inputs bar, keystroke → on screen timing; 20 ms debounce, L2 on CPU Medium (warm: 6% miss the 36 ms timeout); QAC in a dropdown under the search box; SERP L1 + L2 results below it and a Search → results bar; storefront card, category icons; Sign in as 3 personas (data's `demo_personas`), decoupled from the clickstream, browser check pending; next: step 7 |
| ml | worktree `~/GitHub/databricks-blueprints-rts-ml`, branch `rts-ml` | QAC L2 v2 served; SERP L2 done: DCNv2 v2 +0.065 NDCG@10 (≈ LightGBM); e2e CPU LightGBM 92 ms vs T4 DCNv2 101 ms p50 at 200; app serves DCNv2: `rts-serp-l2` (T4) READY; whole SERP path from a laptop p50 444 / p95 502 ms (embed 122, L1 216, L2 102 p50) |
| data | no session; work from the main checkout when needed | Done except the full ~97K-query run and replay-before-materialize; `demo_personas` added (2026-11-05); **batch features go stale as the sim history ages** (items ~2026-11-06, users ~2026-11-29; see data/PLAN.md) |

**Rules for running tracks in parallel**
The main checkout (`~/GitHub/databricks-blueprints`) stays on `main` and holds no track's work: each track commits on
its own branch in its own worktree, then merges into `main` there (`git merge rts-app`, then push). New track:
`git worktree add -b <branch> ../databricks-blueprints-<branch> main`, copy `dev.yml` in (below), and `npm install` in
`app/frontend` for the app.
1. `shared/` changes land on `main` with their tests, one session at a time; other tracks merge `main` before
   continuing.
2. Each track edits only its own `PLAN.md`, plus its status line above. Cross-track asks go in the table below.
3. Each bundle is deployed only from its own track's worktree: every worktree deploys to the same workspace path, so
   two deploys of one bundle overwrite each other.
4. Billable resources have one owner (table below). Update the state column before turning anything on or off.
5. Integration checks (browser, scenario Play, step 7, README) run after merging into `main`, with the app deployed
   from `rts-app` once it has merged `main`.

## Shared resources

| Resource | Owner | State |
|---|---|---|
| `rts-vs-endpoint` + `products_hybrid_idx` (Standard; bills hourly) | ml | ONLINE (kept overnight: the workspace is at its 500-endpoint quota) |
| `rts-query-embedding` (T4, route-optimized) | ml | READY (recreated 2026-11-05 08:46) |
| `rts-qac-l2` (CPU Medium, route-optimized, no scale-to-zero) | ml | READY (recreated 2026-11-05 08:46) |
| Session pipeline `b2e71ddb-8541-4ffa-8c4e-82ecbeca22da` (continuous, Real-Time Mode → Lakebase session tables) | data | RUNNING (restarted 2026-11-05 08:38) |
| Kafka ingestion pipeline `1fa949e7-2477-4306-9898-66f992328abf` (continuous, topic → `session_events_ingest`) | data | Stopped 2026-11-05 (end of day; was found running); only training/offline features need it, not the app |
| Hourly user/item batch materializations (Feature Store-managed jobs, ~7 min/run, 6 jobs) | data | Running hourly. `update-materialized-feature --pipeline-schedule-state PAUSED` does **not** stop them (tested 2026-11-06 on `item_carts_7d`: the 07:00 UTC run still fired; restored to ACTIVE). `time_shift` runs them by hand (`run_now`) |
| Lakebase `rts-online-store` (online store, QAC prefixes, demo scenarios) | data | Running (min 4 / max 8 CU, no auto-suspend: bills 24/7) |
| App `rts-search` (MEDIUM compute; shared with a colleague, CAN_USE) | app | RUNNING (restarted 2026-11-05 08:38; bundle redeployed for CAN_QUERY) |
| `rts-serp-l2` (T4, route-optimized; DCNv2) | ml | READY (recreated 2026-11-05 08:47) |
| `rts-serp-l2-lgbm` (CPU, route-optimized; LightGBM comparator) | ml | Deleted 2026-11-04 after the benchmark |

- **Stop billable resources when not testing (the user's rule).** Serving endpoints can't be stopped, only deleted
  (Express GPU rebuild ~5 min): `rts-qac-l2` (CPU) and `rts-query-embedding` (T4); `rts-vs-endpoint` bills hourly too.
  **Never scale-to-zero (the user, 2026-11-02):** cold starts take minutes against millisecond budgets, so QAC would
  silently fall back to L1 and SERP embedding would time out. Endpoints are always on while in use, deleted otherwise.
- `rts-vs-endpoint` bills hourly; the workspace sits at its 500-endpoint quota, so deleting it may mean waiting for a slot.
  `scripts/post_demo.sh` keeps it (and Lakebase); it deletes the serving endpoints and stops the app and both pipelines.
- **The e2-demo-fe cleanup bot (`fe-infra-management`, SP `57e7dc57-…`) changes our resources.** It deleted
  `products_hybrid_idx` at 2026-11-06 02:37 UTC and turns scale-to-zero on for all three serving endpoints every night
  (~02:45 UTC). `scripts/pre_demo.sh` repairs both: it rebuilds a missing index (`serp_l1_ai_search`), and
  `ensure_endpoint` now treats scale-to-zero as a difference (turning it back off). Its checks fail on either. Lasting
  fix: an FE infra exception (go/feinfra-jira), not filed yet.
- Feature Store-managed pipelines and jobs don't appear in `databricks pipelines list-pipelines` or `jobs list`, and
  `pipeline_schedule_state: ACTIVE` doesn't mean the pipeline runs. Get their IDs from
  `databricks api get /api/2.0/feature-engineering/materialized-features/<id>` (the CLI command drops them).
- With the session pipeline stopped, Kafka writes still succeed but Play fails with "Not every feature caught up
  within 15 s (landed: none)" (2026-11-03).
- Kept on overnight: `rts-vs-endpoint` (quota) and Lakebase. The 6 synced-table pipelines are triggered and idle.

**Pause at end of day:**
1. `databricks pipelines stop b2e71ddb-8541-4ffa-8c4e-82ecbeca22da -p e2-demo-fe` (session)
2. `databricks pipelines stop 1fa949e7-2477-4306-9898-66f992328abf -p e2-demo-fe` (ingestion)
3. `databricks serving-endpoints delete rts-query-embedding -p e2-demo-fe`, `... delete rts-qac-l2 -p e2-demo-fe`, and `... delete rts-serp-l2 -p e2-demo-fe`
4. `databricks apps stop rts-search -p e2-demo-fe` (this also cuts off anyone it's shared with)
5. Update the state column above.

**Before a demo (resume after a pause):** from this folder, `scripts/pre_demo.sh` (~15 min). It does the steps below
plus `demo_job` (`time_shift` moves the simulated history to end yesterday so batch features are full strength, then
re-picks personas), and ends with `scripts/pre_demo_checks.py`: endpoints READY, VS ONLINE, query embedding, batch
features current, `/api/checks/personas`, and one Play landing in the session tables. Logs: `/tmp/rts_pre_demo/`.
What it runs, for doing a step by hand:
1. In parallel:
   - `cd ml && databricks bundle deploy -t dev -p e2-demo-fe`, then (`--no-wait`):
     - `databricks bundle run qac_l2_job -t dev -p e2-demo-fe --only qac_l2_endpoint` (serves the latest
       `qac_ranker_lgbm`; no retraining)
     - `databricks bundle run retrieval_job -t dev -p e2-demo-fe --only serp_query_embedding` (serves the latest
       `gte_large_en_v1_5_raw` on a T4, Express, ~5 min; no GPU logging needed)
     - `databricks bundle run serp_l2_job -t dev -p e2-demo-fe --only serp_l2_endpoint` (DCNv2 on a T4, ~7 min)
   - `databricks apps start rts-search -p e2-demo-fe`
   - `databricks pipelines start-update b2e71ddb-8541-4ffa-8c4e-82ecbeca22da -p e2-demo-fe` (session; ~70 s to
     RUNNING, then ~3 min to catch up on the backlog before Play works)
   - Ingestion only when regenerating training/offline data:
     `databricks pipelines start-update 1fa949e7-2477-4306-9898-66f992328abf -p e2-demo-fe`
2. Check from this folder: `uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I
   ml/scripts/check_query_embedding.py shared/configs/dev.yml` prints OK, and `databricks serving-endpoints get
   rts-qac-l2 -p e2-demo-fe` is READY. `rts-vs-endpoint` should be ONLINE.
   Then `cd app && databricks bundle deploy -t dev -p e2-demo-fe`: deleting an endpoint drops its permissions, so the
   app SP has no `CAN_QUERY` on the recreated ones until the app's resources are re-applied (2026-11-04: L2 and
   custom embeddings would silently fall back).
3. About 4 min after starting the session pipeline, play one throwaway scenario and check that the freshness panel
   fills in.

## Contracts (`shared/`)

Checked by tests, not by sessions agreeing. Change one only under rule 1.

| Contract | Owner | Used by |
|---|---|---|
| Config schema (`rts_shared.config`, `shared/configs/example.yml`) | all (rule 1) | all |
| `EVENT_FIELDS`, `event_payload` (`rts_shared.events`) | data | app (scenario Play) |
| Feature keys (`rts_shared.features`) | data | ml |
| `SCENARIO_COLUMNS`, `replay` (`rts_shared.scenarios`) | data | app |
| `QAC_PREFIX_COLUMNS`, `lookup_prefix`, `filter_completions` (`rts_shared.retrieval`) | ml | app |
| `QAC_L2_CANDIDATE_COLUMNS`, `QAC_L2_REQUEST_COLUMNS`, `qac_l2_records` | ml | app |
| `SERP_INDEX_COLUMNS`, `SERP_INDEX_TEXT` | ml | app |
| Kafka client and Lakebase helpers (`rts_shared.kafka`, `rts_shared.lakebase`) | data | app, ml |

## Cross-track asks

| Ask | From | To | State |
|---|---|---|---|
| Online table names for the session features (session panel) | app | data | done by the app: found in Postgres database `main` (named after the catalog) via `pg_catalog` |
| Possible train/serve skew: online `sess_last_event_type` is the last click/cart (2026-11-03: sc1 ends `back`, sc5 `search`, both online `click`), but `feature_specs` defines it over all events. Check what training saw | app | data, ml | ml answered 2026-11-04: training always saw `search` (offline `ColumnSelection` includes the event at the label time; window counts exclude it), so the skew is real. SERP L2 drops the feature; QAC can't be affected (constant in training, LightGBM never split on it). Open for data: intended semantics |
| `rts-qac-l2` session lookups checked from the app via scenario Play | ml | app | open |
| Wire SERP L2 into `/api/search`: L1 hits → `serp_l2_records` (session, user, candidates) → endpoint under the 200 ms `serp_l2` timeout, falling back to the L1 order. Model: DCNv2 v2 on the T4 (the user, 2026-11-03). `rts-serp-l2` is READY (2026-11-03) | ml | app | done 2026-11-04 (deployed; 200-candidate call from the app still to check in the browser) |

## Where things are
- Local code: this folder. Bundles deploy to `/Workspace/Users/jon.cheung@databricks.com/real-time-search/<bundle>/files/`
  (the repo layout is kept under `files/`).
- Config: `shared/configs/dev.yml` (gitignored; synced by the bundles anyway). Jobs pass
  `shared/configs/<bundle target>.yml` as `config_path`; interactive notebooks default to `dev.yml`.
- Data and features: `main.jon_cheung`. Online store / Lakebase: `rts-online-store`. Kafka: connection
  `jon_cheung_rts_kafka`, topic `jon_cheung_rts_events` on the shared `realtimemode` MSK.
- Superseded but kept (the user chose to keep them for now; ask before deleting): the old flat files in
  `/Workspace/Users/jon.cheung@databricks.com/real-time-search/` (`00_*`–`02_*`, `common.py`, `configs/`, ...).
  `_scratch/` was deleted 2026-11-01. UC objects didn't move.
- Worktrees: gitignored files (`shared/configs/dev.yml`) aren't in a new worktree, so each has a **copy**. The main
  checkout's is the source of truth: change config there, then copy it to the worktrees and redeploy the bundles that
  read it. Check they match, from the main checkout's `real-time-search/`:
  `for w in rts-app rts-ml; do cmp shared/configs/dev.yml ~/GitHub/databricks-blueprints-$w/real-time-search/shared/configs/dev.yml; done`.

## How we work
- Tests first for local logic, from this folder: `uv run --no-project --with pytest --with pyyaml --with pandas --with numpy
  --with lightgbm --with scikit-learn --with mlflow-skinny --with databricks-sdk python -m pytest -q` (109 pass, 2026-11-03).
- Deploy and run per bundle: `cd data && databricks bundle deploy -t dev -p e2-demo-fe`, then
  `databricks bundle run <job> -t dev -p e2-demo-fe [--params k=v]` (`--no-wait` for long runs). Read results from
  `dbutils.notebook.exit` JSON with `jobs get-run-output <task run_id>`.
- `bundle validate --strict` always fails on one false-positive warning: `sync.include` for the gitignored configs
  "does not match any files", but deploy uploads them. Use plain `validate`.
- Every notebook ends with checks that assert, so a green run means the step's claim held.
- Update your track's `PLAN.md` at the end of each step. Commit on your track's branch.

## Gotchas (all tracks)
- Don't symlink `dev.yml` into a worktree: `bundle deploy` skips symlinks and **deletes** the deployed `dev.yml`
  (2026-11-03: "1 deleted", then jobs had no config until a copy was redeployed).
- On serverless, `psycopg` aborts Python (SIGABRT) while importing its native extension, intermittently, even with
  `[binary]`; serverless task retries hide it. Notebooks use `pg8000` (pure Python), or Spark SQL on the UC table.
- The `e2-demo-fe` CLI token expires, and the IP ACL can block calls while off VPN.

## Integration

### Step 7: Benchmark and README
- [ ] README: Question / Takeaway (measured numbers) / How to run; cleanup steps

## Decisions that changed the original plan

| Date | Change | Why |
|---|---|---|
| 2026-10-29 | Categories classified per query, with descriptions and instructions; products take a vote | ~10x fewer LLM calls; overlapping departments split consistently |
| 2026-10-29 | 10K-query subset for iteration; resumable streaming classification | `ai_classify` throughput (~6.5 rows/s) |
| 2026-10-29 | Simulator: lower click rates, brand-aware queries, within-search intent checks | First version had 92% engagement and almost no brand signal |
| 2026-10-29 | Sim dates relative to the run | Fixed past dates left online user/item windows empty |
| 2026-10-29 | Composite-key category features (from the FV NBO accelerator) | 40 features become 2; per-candidate affinity for free |
| 2026-10-29 | Route-optimized L2 endpoints | QAC 40 ms budget |
| 2026-10-29 | Session events via Kafka (shared MSK), not Zerobus to Delta | Sub-second freshness required; Delta measured p50 158 s |
| 2026-10-29 | Dropped `item_impressions_7d` | `session_events` has clicks, not impressions |
| 2026-10-30 | Event times at millisecond precision everywhere (simulator floors emitted times) | Kafka payloads carry ms; µs label times let each search count in its own features (200/200 point-in-time mismatches, all +1 search) |
| 2026-10-30 | Restructure into `data/`, `ml/`, `app/` bundles + `shared/` contracts | Mirrors DE / ML / frontend ownership; contracts made explicit and tested |
| 2026-11-01 | `02` freshness check waits for a readiness probe and asserts | The first Kafka run produced probes before the pipeline's flows started; 0 landed and the check passed silently |
| 2026-11-01 | QAC popularity counts all train searches; completions below a min CTR are dropped (`qac.drop_dead_ends`) | Popularity measures what users submit; engaged-only counts would apply engagement twice next to `ctr` |
| 2026-11-01 | QAC synced table in `main.jon_cheung` (Postgres db `databricks_postgres`), snapshot mode, rerun re-snapshots | No extra Lakebase catalog; the table is rebuilt in full each run |
| 2026-11-01 | Restructured into `data/`, `ml/` bundles + `shared/` contracts; Zerobus config removed | Team ownership and explicit contracts; Kafka replaced Zerobus |
| 2026-11-03 | QAC L2 timeout 25 → 36 ms (40 ms budget - L1 p95 3.7 ms, both measured from the app) | Serving has a ~25 ms per-call floor from the app (p50 28, p95 33–38 ms); 25 ms timed out on nearly every call |
| 2026-11-04 | QAC end-to-end target: keystroke → on screen p95 ≤ 120 ms = the 20 ms debounce + ≤ 100 ms for network, server (40 ms budget) and render (the user) | ~100 ms is where a response feels instant; the debounce applies to the keystroke before a pause, the one whose suggestions the user reads, so it counts. Estimated ~113 ms at 20 ms (80 ms reading: network 53, server 34, render 6); network depends on the user's distance from the region |
| 2026-11-04 | QAC debounce 80 → 20 ms; QAC L2 (LightGBM) serves on CPU **Medium**, never Small (the user) | The 80 ms debounce was ~82 of ~176 ms keystroke → on screen. At 20 ms the app sends ~1 L2 call per keystroke (~2x a fast typist's requests at 80 ms), so per-keystroke QPS sizes the endpoint: on Small the calls queued (6/18 over the 36 ms timeout vs 1/8). Size it for peak keystroke QPS, not search QPS |
