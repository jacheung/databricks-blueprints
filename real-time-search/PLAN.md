# Build plan and status

Living checklist for this blueprint. A box is ticked only when its check has passed on Databricks
(workspace `e2-demo-fe`, `main.jon_cheung`) or, for local code, in `pytest`. Updated at the end of every step.
Deferred scope lives in [PARKING_LOT.md](PARKING_LOT.md).

## Start here (new session)

**Status as of 2026-11-03:** services resumed and checked (`check_query_embedding.py` OK, `rts-qac-l2` READY,
`rts-vs-endpoint` ONLINE). `/api/search` is written and tested locally (below); not deployed yet.

**Status as of 2026-11-02 (end of day):** Steps 0–2, QAC L1 and the app (L1 only) are done and verified on Databricks.
- **QAC L2:** `main.jon_cheung.qac_ranker_lgbm` v2 trained and registered (MRR@5 +0.059 overall, +0.101 on 2nd+
  searches, -0.015 on no-intent sessions; table in step 5). The user chose to ship it with no gate; the no-intent
  trade-off goes in the README. Parked: gate B / feature C (step 5).
- **SERP L1:** hybrid recall@200 0.876. Query embedding moves off pay-per-token to `rts-query-embedding` (T4,
  `gte_large_en_v1_5_raw`), verified to return the same results (step 3).
- **Paused overnight (the user, 2026-11-02):** `rts-qac-l2` and `rts-query-embedding` deleted (models stay registered);
  the session pipeline is stopped. `rts-vs-endpoint` + `products_hybrid_idx` kept (deleting risks the 500-endpoint quota).

**Resume, first thing, before any next step:** turn everything back on, then check it.
1. `cd ml && databricks bundle deploy -t dev -p e2-demo-fe`, then in parallel (`--no-wait`):
   - `databricks bundle run qac_l2_job -t dev -p e2-demo-fe --only qac_l2_endpoint` (serves the latest
     `qac_ranker_lgbm`; no retraining)
   - `databricks bundle run retrieval_job -t dev -p e2-demo-fe --only serp_query_embedding` (serves the latest
     `gte_large_en_v1_5_raw` on a T4, Express, ~5 min; no GPU logging needed)
2. Check from this folder: `uv run --no-project --with "databricks-sdk>=0.81" --with pyyaml python -I
   ml/scripts/check_query_embedding.py shared/configs/dev.yml` prints OK, and `databricks serving-endpoints get
   rts-qac-l2 -p e2-demo-fe` is READY. `rts-vs-endpoint` should be ONLINE.
3. Start the session pipeline only when testing scenario Play or freshness:
   `databricks pipelines start-update b2e71ddb-8541-4ffa-8c4e-82ecbeca22da -p e2-demo-fe`.

**Next (after resuming): app wiring.**
- **SERP:** `/api/search` embeds the query on `rts-query-embedding`, then `query_index(query_text=..., query_vector=...,
  query_type="HYBRID", num_results=200)`; declare both endpoints as app resources (`CAN_QUERY`).
- **QAC L2:** `/api/qac` calls `rts-qac-l2` with `qac_l2_records` under the 25 ms timeout, falling back to L1.
- Both endpoints are route-optimized, so the app's service principal must use OAuth (notebooks can't query them).
- Then step 7 times embed, search and L2 separately.

Open items:
- Measure the Lakebase lookup p95 from the app (step 7). psycopg loads cleanly in the Apps container (2026-11-02).
- Check the app's Lakebase SELECT survives a snapshot re-sync (synced tables may be recreated; default privileges are set).
- `rts-vs-endpoint` bills hourly; the workspace sits at its 500-endpoint quota, so deleting it may mean waiting for a slot.
- **Stop billable resources when not testing (the user's rule).** Serving endpoints can't be stopped, only deleted
  (Express GPU rebuild ~5 min): `rts-qac-l2` (CPU) and `rts-query-embedding` (T4); `rts-vs-endpoint` bills hourly too.
  **Never scale-to-zero (the user, 2026-11-02):** cold starts take minutes against millisecond budgets, so QAC would
  silently fall back to L1 and SERP embedding would time out. Endpoints are always on while in use, deleted otherwise.
- The session streaming pipeline (`b2e71ddb-8541-4ffa-8c4e-82ecbeca22da`) runs continuously; stop it when not testing.
  Stopped 2026-11-01 (IDLE). Restart with `databricks pipelines start-update <id> -p e2-demo-fe` before freshness checks or the app.
- `kafka_stream` replays history before `feature_views` materializes, so the session online tables hold only events
  produced after the pipeline started. Fine for probes and live traffic; revisit before the app demo (step 4).
- `preflight` records FAIL checks but doesn't assert, so `catalog_job` continues past a failed preflight.
- The full ~97K-query catalog run is still blocked on `ai_classify` throughput (step 1).

**Where things are**
- Local code: this folder. Bundles deploy to `/Workspace/Users/jon.cheung@databricks.com/real-time-search/<bundle>/files/`
  (the repo layout is kept under `files/`).
- Config: `shared/configs/dev.yml` (gitignored; synced by the bundles anyway). Jobs pass
  `shared/configs/<bundle target>.yml` as `config_path`; interactive notebooks default to `dev.yml`.
- Data and features: `main.jon_cheung`. Online store / Lakebase: `rts-online-store`. Kafka: connection
  `jon_cheung_rts_kafka`, topic `jon_cheung_rts_events` on the shared `realtimemode` MSK.
- Superseded but kept (the user chose to keep them for now; ask before deleting): the old flat files in
  `/Workspace/Users/jon.cheung@databricks.com/real-time-search/` (`00_*`–`02_*`, `common.py`, `configs/`, ...).
  `_scratch/` was deleted 2026-11-01. UC objects didn't move.

**How we work**
- Tests first for local logic, from this folder: `uv run --no-project --with pytest --with pyyaml --with pandas --with numpy
  --with lightgbm --with scikit-learn --with mlflow-skinny --with databricks-sdk python -m pytest -q` (109 pass, 2026-11-03).
- Deploy and run per bundle: `cd data && databricks bundle deploy -t dev -p e2-demo-fe`, then
  `databricks bundle run <job> -t dev -p e2-demo-fe [--params k=v]` (`--no-wait` for long runs). Read results from
  `dbutils.notebook.exit` JSON with `jobs get-run-output <task run_id>`.
- `bundle validate --strict` always fails on one false-positive warning: `sync.include` for the gitignored configs
  "does not match any files", but deploy uploads them. Use plain `validate`.
- Every notebook ends with checks that assert, so a green run means the step's claim held.
- Update this file at the end of each step. Nothing is committed yet.

**Gotchas we've hit**
- Feature deletes: delete materializations before the feature. For batch features, delete the offline one, which
  removes the online one too.
- Don't `DELETE` from a source table under live streams. Rebuilding `session_events` (`clickstream_job` with
  `rebuild_data=true`) breaks them, so run `teardown_job` with `confirm=yes` first.
- On serverless, `psycopg` aborts Python (SIGABRT) while importing its native extension, intermittently, even with
  `[binary]`; serverless task retries hide it. Notebooks use `pg8000` (pure Python), or Spark SQL on the UC table.
- Freshness is measured server-side (`commit_time - event_time`, `is_timer = false`), within the 30-minute window.
- A new streaming pipeline reads Kafka from the latest offset and its flows start minutes after creation. `feature_views` now
  waits for a readiness probe to land before measuring, and asserts that every probe landed and p95 < 1 s.
- The `e2-demo-fe` CLI token expires, and the IP ACL can block calls while off VPN.

**Question:** Does a session-aware L2 reranker improve QAC and SERP relevance enough to justify its latency, and can
Databricks serve both within real-time budgets (QAC < 40 ms, SERP < 400 ms, feature freshness < 1 s)?

## Checklist

### Step 0: Config and preflight
- [x] `shared/configs/example.yml` + gitignored `shared/configs/dev.yml`; `rts_shared.config.load_config` validates it
- [x] `preflight`: auth, catalog on own storage, FE >= 0.16, online store API, embedding endpoint (all pass)

### Step 1: Data (10K-query subset, `data.max_queries`)
- [x] `catalog_prep`: ESCI to `products` (170K), `queries` (10K), `esci_labels` (187K); categories via `ai_classify` with descriptions and instructions
- [x] Resumable classification (Structured Streaming `availableNow` + checkpoints); resume verified
- [x] `clickstream_sim`: 200K sessions; engaged-search share 57%; intent brand lift 38% shown vs 53% clicked; price-gap lift 0.66 vs 0.42; CTR E > S > C > I
- [x] Sim dates relative to the run date, so online user/item features have data
- [ ] Full ~97K-query run. Blocked: `ai_classify` runs at ~6.5 rows/s, so a full run takes hours. Decide between `ai_classify`, `ai_query` with a fast model, or skipping the title fallback.

### Step 2: Clickstream Feature Views
- [x] `features.py`: 18 features (12 session, 4 user, 2 item); category affinity keyed on (entity, `category`)
- [x] User/item features on Delta + cron, offline + online; online tables populated
- [x] Point-in-time training values: 0/200 mismatches; features see only events strictly before the label time
- [x] Delta-source freshness baseline: p50 158 s, p95 186 s, 0/18 under 1 s. Too slow, so session events move to Kafka
- [x] `kafka_stream`: UC Kafka connection, topic on shared `realtimemode` MSK, FE Stream, history replayed and ingested (994,720 events)
- [x] Session features on the Kafka `StreamSource`, re-registered and materialized with `StreamingMode`
- [x] Kafka freshness: p50 58 ms, p95 442 ms (= max), min 38 ms; 18/18 under 1 s, 17/18 under 200 ms (20 probes, 2 warm-up)
  - Rerun from the bundle (2026-11-01): p50 58 ms, p95 117 ms (= max), min 30 ms; 18/18 under 200 ms

### Restructure into bundles
- [x] `shared/` (config, event and feature contracts, Kafka), `data/` bundle (3 jobs), `ml/` bundle (no jobs yet); 68 tests pass incl. the feature-contract test
- [x] `rts-data` deployed; `clickstream_job` (`rebuild_data=false`): sim skipped, stream and features reused, point-in-time 0/200, freshness passes
- [x] `teardown_job` (`confirm=no`) lists 18 features, 18 materializations and the stream; deletes nothing
- [x] Deleted workspace `_scratch/`; the old flat workspace files are kept for now

### Step 3: L1 retrieval
- [x] `qac.py`: prefix expansion, top-k, completion features, lookup/normalize (tested locally)
- [x] Retrieval contract in `rts_shared.retrieval` (prefix table + index columns), tested; `qac.drop_dead_ends` (min CTR); 72 tests pass
- [x] `ml/notebooks/qac_l1_lakebase` (in `retrieval_job`): 9,743 completions, 8,852 served at `min_ctr` 0.05; 92,406 rows / 59,850 prefixes;
  snapshot-synced to `main.jon_cheung.qac_prefix_completions_online` on `rts-online-store`; row counts and 200 sampled lookups match Delta
  - Lookup latency from the notebook (indicative only): psycopg p50 2.9 / p95 3.7 ms; pg8000 p50 8.8 / p95 10.7 ms. The budget check is step 7
- [x] `ml/notebooks/serp_l1_ai_search`: hybrid index over products; recall@200 on ESCI Exact labels (run
  `640913679989299` green, 1,000 test queries): hybrid recall@50/100/200 0.744 / 0.824 / **0.876**; vector-only 0.723 /
  0.806 / 0.864. 22 queries have no Exact in the top 200. Indicative latency from the notebook: hybrid p50 586 / p95
  1,114 ms, vector-only p50 296 / p95 584 ms. **Both are over the 400 ms SERP budget before L2;** measure from the app
  (step 7) before drawing conclusions (Standard endpoint, notebook network path)
  - 2026-11-02, from a laptop over OAuth: the managed-embedding index accepts `query_vector` (ANN, and HYBRID with
    `query_text` + `query_vector`); results match the `query_text` path exactly (200/200, same top 5, 5 queries), so
    **no index rebuild is needed to embed queries elsewhere**. Pay-per-token embedding took 166–812 ms; passing the
    vector saved ~30–110 ms per query. Absolute numbers include the laptop's WAN round trip; measure from the app.
  - Plan (the user, 2026-11-02): embed queries on a custom T4 endpoint serving the same gte-large-en-v1.5 (re-log the
    serving-bench `EmbeddingModel` pyfunc on GPU compute; its old `sb-*` endpoints are deleted). Check cosine ≈ 1 vs
    pay-per-token and identical top-200 before switching. Serving-bench at 256 tokens, concurrency 1: T4 p50 104 /
    p95 124 ms (12 RPS max) vs pay-per-token p50 80 / p95 853 ms; real queries are 3–8 tokens
  - `rts-query-embedding` (T4, Express) was READY ~5 min after creation (no image build). Serving serving-bench's
    `gte_large_en_v1_5` v3 failed parity: **pay-per-token GTE returns unnormalized vectors (norm ~24) and the index
    ranks by L2**; serving-bench's pyfunc normalizes, so cosine was 0.999999 but hybrid overlap 4.5% (99.5% once
    rescaled). Fix: `query_embedding_model` (Serverless GPU A10, AI env v5) logs raw vectors as
    `gte_large_en_v1_5_raw` with Express env_pack, asserting cosine and norm vs pay-per-token before registering;
    `check_query_embedding.py` now asserts the norm too. Run `345583074492262` green (the GPU task's first attempt
    timed out waiting 900 s for Serverless GPU compute; the automatic retry passed).
  - [x] `rts-query-embedding` serves `gte_large_en_v1_5_raw` v1 and can replace managed query embeddings
    (`check_query_embedding.py`, 200 test queries): cosine 0.999999, norm gap ≤ 0.02% (norm ~24.2), hybrid top-200
    overlap 0.997 mean / 0.985 min. Hybrid isn't deterministic at the tail: the same text query twice overlaps 0.990
    min / 0.9992 mean, so the check allows mean ≥ 0.99, min ≥ 0.97. T4 embed from a laptop (WAN included): p50 91 /
    p95 104 ms vs pay-per-token 166–812 ms. Next: the app embeds on the T4 and passes `query_text` + `query_vector`;
    step 7 times embed and search separately
  - serving-bench's normalized `main.jon_cheung.gte_large_en_v1_5` (v1–v3) is kept (the user, 2026-11-02): unused here,
    nothing serves it, storage only
  - Written (title + brand text, hybrid vs vector-only, k = 200); `products_search` built. Was blocked on the 500-endpoint
    quota; Akil Thomas freed two slots 2026-11-02. `rts-vs-endpoint` (Standard) created 23:35Z on the third retry: the
    quota freed ~25 min after the deletes, and slots go fast. The notebook now claims the endpoint before the source build
- [ ] `ml/resources/retrieval.job.yml` deployed and green

### Step 4: App, L1 only
Demo design (2026-11-02, with the user): no login or product click-through. Replay a **demo scenario** (the start of a
test-split simulated session) to Kafka under a fresh session id, then type in one search bar with two columns:
Static (L1) and Session-aware (L1 + L2). Only the click sequence is pre-baked; features and rankings are live.
- [x] `demo_scenarios` (data, `demo_job`): 6 scenarios (3 logged-in, 3 guests), synced to `demo_scenarios_online`
- [x] `rts_shared.scenarios.replay`, `rts_shared.lakebase` (sync + pg8000 connect), prefix lookup moved into
  `rts_shared.retrieval`; QAC notebook refactored onto them and rerun green (first attempt); 84 tests pass
- [x] `app/` bundle `rts-app`: FastAPI (`/api/qac`, `/api/scenarios`, `/api/scenarios/{id}/play`) + React two-column UI;
  app `rts-search` running; psycopg[binary] + pool loads fine in the Apps container; `grants_job` gives its SP SELECT
- [ ] Verified in the browser: scenarios list, QAC columns, Play produces to Kafka (SP access to the MSK service credential untested)
- [ ] Session panel: read the session's online features back after Play (needs the online table name from Feature Views)
- [x] `/api/search` in code (2026-11-03): `rts_app.service.search` embeds on `rts-query-embedding`, then runs
  `query_index(query_text, query_vector, HYBRID, k=200, query_columns=[search_text])`; `mode=ANN` sends the vector
  only. If our endpoint fails, it sends text only (the index embeds on pay-per-token) and returns
  `embedding: fallback_managed`. Returns embed and search timings separately. 7 tests; 109 pass. Smoke-tested
  from a laptop against the live endpoints (HYBRID, ANN, fallback: 200 hits each)
  - Verified 2026-11-03 (mismatched probe: text "usb c charging cable", vector of "dry dog food"): on our
    managed-embedding index, HYBRID uses **our vector for the ANN half and the text only for the keyword half** (top
    200 = 111 dog-food ANN hits + 89 USB keyword hits). ANN rejects both ("Only one of Field 'query_text' or Field
    'query_vector'"). Not proven: that the index skips the pay-per-token call when given a vector (check
    `system.serving.endpoint_usage` if it matters)
- [ ] `/api/search` deployed: `rts-query-embedding` app resource (`CAN_QUERY`), SP `SELECT` on `products_hybrid_idx`;
  UI results column

### Step 5: QAC L2 (LightGBM, CPU, route-optimized)
Design agreed 2026-11-02, following the Feature Views best-practice blog
(community.databricks.com/t5/technical-blog/from-batch-to-milliseconds-real-time-feature-views-for/ba-p/170806):
every input the model reads is a declared Feature from one of three sources, so training (`create_training_set(df,
features=...)`, point-in-time) and serving (`fe.log_model(flavor=mlflow.pyfunc, training_set=...)`; the endpoint looks up
stream and Delta features itself) share one definition. The app sends only keys and request fields.

| Inputs | Source | Keys |
|---|---|---|
| Session: `sess_n_clicks/carts/searches_30m`, `sess_avg/min/max/std_price_30m`, `sess_last_price/brand/category/event_type` | `StreamSource` (Kafka) | `session_id` |
| Session x candidate: `sess_cat_cnt_30m` | `StreamSource` | (`session_id`, candidate `category`) |
| User: `user_median_price_30d`, `user_n_clicks/carts_30d`; `user_cat_cnt_30d` | `DeltaTableSource` (cron) | `user_id`; (`user_id`, candidate `category`) |
| Candidate (from the L1 row): `l1_rank`, `pop_30d`, `pop_7d`, `ctr`, `category_share`, `brand_share`, `median_clicked_price`, `n_tokens`, `prefix_len`, `is_word_boundary`, `category`, `brand` | `RequestSource` | request fields |

Crosses are computed **inside the pyfunc model** (like the blog's model), from the features it receives, then
LightGBM scores: `sess_cat_share`, `cat_is_last`, `brand_is_last`, `user_cat_share`, `price_gap_session`,
`price_gap_user`, `is_guest`. One function, part of the logged model, so training and serving can't drift. No
`CustomUDF`/`FeatureViewSource` (the SDK has them; not needed). Brand and category identities never reach LightGBM
(no one-hot): it learns "matches the session" for any brand.
Left out: item features (SERP's), raw category. Brand option (b) (`sess_brand_cnt_30m` on (session_id, brand)) only if
the slices show category lift without brand lift. Candidate stats stay `RequestSource` (the app already holds them);
a `DeltaTableSource` keyed on `completion` would add ~20 lookups inside the 25 ms budget.

- [x] QAC table: completion `brand` + `brand_share` (top clicked brand among the completion's train clicks; share
  among clicks with a known brand). Rerun green 2026-11-02: 8,616/8,852 served completions have a brand; Lakebase
  matches Delta on 200 lookups; `grants_job` rerun green (app SP has SELECT on the re-synced table)
- [x] Training set: ~50K train searches x prefixes of 1-3 chars x L1 top 20; label = submitted completion; groups with
  no positive dropped from training (still L1/L2 misses in eval). Validate on the last train days; test = test split.
  Confirmed: `RequestSource` + composite keys work in `create_training_set`. 67,154 train / 7,052 valid / 29,964
  test groups; session features present on 34% of rows (`sess_last_category`: sessions with an earlier engaged event)
- [x] Local logic, test-first (`rts_ml.qac_l2`, 14 tests): crosses, `model_matrix` (no identities), `fit_ranker` beats
  L1 on synthetic intent data, `rank_metrics`, paired `bootstrap_ci`, `slice_report`; `rts_ml.qac_l2_model.QacRanker`
  pyfunc round-trips through `mlflow.pyfunc.save_model` with `code_paths`. Candidate fields are a shared contract
  (`rts_shared.retrieval.QAC_L2_CANDIDATE_COLUMNS`)
- [x] SDK (0.18.2): serving supports only models whose inputs are all Feature View features, so the candidate fields
  must be `RequestSource` features (`RequestSource.from_dataframe` + one `ColumnSelection` each, named after the column)
- [x] `ml/notebooks/qac_l2_lgbm.py` (`qac_l2_job`) green (run `415213954327977`, 2026-11-02): `LGBMRanker` (lambdarank, 31 leaves, lr 0.05, early stop on
  NDCG@5) in the pyfunc; `fe.log_model`. Samples 30K train / 10K test searches (config), not 50K: ~1.8M rows to pandas
- [x] Eval, L1 vs L2, MRR@5 + success@1 with bootstrap 95% CIs, per slice: first vs 2nd+ search; guest vs logged-in;
  prefix 1/2/3; intent none / category / category+brand. Plus the ceiling (submitted query in L1 top 20). Go/no-go
  before the endpoint
  - Results (test split, paired bootstrap 95% CI), `qac_ranker_lgbm` v1, best iteration 270. Ceiling 81.6%:

    | Slice | n | MRR@5 L1 → L2 (lift, CI) | success@1 L1 → L2 (lift) |
    |---|---|---|---|
    | all | 29,964 | 0.539 → 0.598 (+0.059, 0.056–0.061) | 0.458 → 0.521 (+0.064) |
    | first search | 15,545 | 0.540 → 0.560 (+0.020) | +0.019 |
    | 2nd+ search | 14,419 | 0.538 → 0.639 (+0.101) | +0.112 |
    | guest / logged-in | 11,801 / 18,163 | +0.045 / +0.068 | +0.051 / +0.072 |
    | prefix 1 / 2 / 3 | ~10K each | +0.074 / +0.066 / +0.036 | +0.075 / +0.072 / +0.045 |
    | intent none | 9,092 | 0.568 → 0.553 (**-0.015**, -0.019 to -0.012) | -0.018 |
    | intent category | 8,300 | +0.060 | +0.066 |
    | intent category+brand | 12,572 | 0.488 → 0.599 (+0.111) | +0.121 |

    Brand lift shows (category+brand is the biggest slice lift), so brand option (b) isn't needed. No-intent sessions
    regress slightly: L2 reads intent into random clicks. Go/no-go: the user's call
- [ ] Endpoint `rts-qac-l2` (CPU, route-optimized): online lookups work; p95 within the 25 ms L2 timeout at 20
  candidates (else 10)
  - Created 2026-11-02 serving v1 (READY). `qac_l2_endpoint` (2nd task of `qac_l2_job`) deploys and asserts READY only:
    **route-optimized endpoints accept only OAuth tokens, and notebook credentials aren't OAuth** (`ValueError: OAuth
    tokens are not available for runtime authentication`). The CLI's `serving-endpoints query` doesn't use the
    data-plane URL either; the Python SDK's `serving_endpoints_data_plane.query` with the CLI profile works. The
    lookup and latency checks move to the app (its SP uses OAuth) and a local SDK smoke test
  - v1 failed every request: **the endpoint strips lookup keys (`session_id`, `user_id`) before the model**, and
    `is_guest` read `user_id`. Fixed: `is_guest` is a request field (`QAC_L2_REQUEST_COLUMNS`, set by
    `qac_l2_records`) and a `RequestSource` feature; the pyfunc test and the notebook reload check now score keyless
    rows. v2 (run `1076298783852058`): same metrics as v1; `rts-qac-l2` serves v2 and scores
    `qac_l2_records` payloads (SDK smoke test 2026-11-02: guest vs logged-in scores differ, so user lookups work).
    Session lookups get checked from the app (scenario Play), latency in step 7
- [ ] App: `/api/qac` calls L2 with the timeout and falls back to L1; serving-endpoint app resource; endpoint-scoped
  OAuth token; the streaming pipeline must run for replayed events to reach features
- [ ] Demo: replayed scenarios flip the right column vs L1
- Known: mild leakage (L1 popularity counts the train searches we train on); the sim's intent never drifts within a
  session, so lift is optimistic vs production

### Step 6: SERP L2 (DCNv2, GPU, route-optimized)
- [ ] Training set, DCNv2, `fe.log_model`; NDCG@10 lift over L1
- [ ] CPU vs GPU latency at 200/500 candidates

### Step 7: Benchmark and README
- [ ] `07_latency_bench`: per-stage p50/p95, L1-only vs L1+L2
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
