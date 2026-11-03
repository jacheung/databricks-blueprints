# ML track: plan and status

QAC L1/L2, SERP L1/L2 and the query-embedding endpoint (`ml/` bundle). Read the top-level [PLAN.md](../PLAN.md) first (question, rules, shared resources, contracts, decisions).
A box is ticked only when its check has passed on Databricks (`e2-demo-fe`, `main.jon_cheung`) or in `pytest`.
Update this file, plus this track's one status line in the top-level file, at the end of every step.

## Status
- **QAC L2:** `main.jon_cheung.qac_ranker_lgbm` v2 trained and registered (MRR@5 +0.059 overall, +0.101 on 2nd+
  searches, -0.015 on no-intent sessions; table in step 5). The user chose to ship it with no gate; the no-intent
  trade-off goes in the README. Parked: gate B / feature C (step 5).
- **SERP L1:** hybrid recall@200 0.876. Query embedding moves off pay-per-token to `rts-query-embedding` (T4,
  `gte_large_en_v1_5_raw`), verified to return the same results (step 3).

## Checklist

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
- Known: mild leakage (L1 popularity counts the train searches we train on); the sim's intent never drifts within a
  session, so lift is optimistic vs production

### Step 6: SERP L2 (DCNv2, GPU, route-optimized)
Design agreed 2026-11-03 (plan: ~/.claude/plans/streamed-fluttering-turing.md):
- Candidates = the L1 hybrid top 200 per search (what the endpoint reranks); label cart 2 / click 1 / else 0.
  Unjudged products are always 0 because the sim never showed them (exposure bias, for the README)
- Inputs are Feature View features only: session (Kafka), user and item (Delta), and `RequestSource` candidate fields
  (`rts_shared.retrieval.SERP_L2_REQUEST_COLUMNS`). Crosses inside the model; no brand/category identities
- Brand features: measure first. Only `sess_last_brand` exists; ask data for (session, brand) / (user, brand) counts
  only if the category+brand slice shows little lift
- DCNv2 (3 cross layers ‖ MLP 128-64), listwise softmax loss, early stop on validation NDCG@10. LightGBM LambdaRank on
  the same matrix as an eval-only comparator
- Trains on Serverless GPU (one A10, AI env v6), no DDP: the data is ~300 MB

- [x] Contract `SERP_L2_CANDIDATE_COLUMNS` / `SERP_L2_REQUEST_COLUMNS` / `serp_l2_records`; `models.serp_l2` training
  settings (commit `fbd53bf`, to land on `real-time-search`)
- [x] Local logic (`rts_ml.serp_l2` pandas only; `rts_ml.serp_l2_dcn` torch; `SerpRanker` pyfunc): 127 default tests +
  4 torch tests. **Torch tests run as a second pytest process (`ml/tests_torch`)**: torch and LightGBM in one process
  segfault on macOS (two OpenMP runtimes; `OMP_NUM_THREADS=1` doesn't help)
- [x] AI Runtime check (`serp_l2_gpu_check`, run `907509776027549`, 2026-11-03): AI env v6 = Python 3.12, torch
  2.11+cu130, A10G; `databricks-feature-engineering` 0.18.2 imports; `SerpRanker` moves to CUDA and scores within
  1e-6 of CPU. **UCDataset: `databricks-uc-dataset==0.0.1b1` isn't on the package index ("from versions: none")**, so
  training reads the table into memory (the user, 2026-11-03: UCDataset is nice to have; single node, small data)
- [x] `serp_l1_candidates` (run `40665446723268`, 2026-11-03): 10,000 train + 5,000 test engaged searches
  (`serp_l2_searches`) cover only **3,675 distinct queries** (popularity-weighted); every one returned the full 200,
  no duplicates or rank gaps: 735,000 rows in `serp_l1_candidates`. Recall of Exact products on the 1,931 test queries
  with Exact labels: @50 0.733 / @100 0.821 / @200 **0.877** (step 3: 0.876 on uniform queries)
- [ ] Training set, DCNv2, `fe.log_model`; NDCG@10 lift over L1 (and LightGBM)
- [ ] Endpoint `rts-serp-l2` (GPU_SMALL, route-optimized) serves with online lookups
- [ ] CPU vs GPU latency at 200/500 candidates
