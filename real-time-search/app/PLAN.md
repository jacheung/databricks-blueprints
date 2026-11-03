# App track: plan and status

FastAPI routes, React UI, app resources and grants, and the latency benchmark (`app/` bundle). Read the top-level [PLAN.md](../PLAN.md) first (question, rules, shared resources, contracts, decisions).
A box is ticked only when its check has passed on Databricks (`e2-demo-fe`, `main.jon_cheung`) or in `pytest`.
Update this file, plus this track's one status line in the top-level file, at the end of every step.

## Status
**Status as of 2026-11-03:** services resumed and checked (`check_query_embedding.py` OK, `rts-qac-l2` READY,
`rts-vs-endpoint` ONLINE). `/api/search` deployed and verified as the app SP; QAC L2 wired with a 36 ms timeout (below).

## Next
**Next (after resuming): app wiring.**
- **SERP:** `/api/search` embeds the query on `rts-query-embedding`, then `query_index(query_text=..., query_vector=...,
  query_type="HYBRID", num_results=200)`; declare both endpoints as app resources (`CAN_QUERY`).
- **QAC L2:** `/api/qac` calls `rts-qac-l2` with `qac_l2_records` under the 25 ms timeout, falling back to L1.
- Both endpoints are route-optimized, so the app's service principal must use OAuth (notebooks can't query them).
- Then step 7 times embed, search and L2 separately.

## Open items
- Measure the Lakebase lookup p95 from the app (step 7). psycopg loads cleanly in the Apps container (2026-11-02).
- Check the app's Lakebase SELECT survives a snapshot re-sync (synced tables may be recreated; default privileges are set).

## Checklist

### Step 4: App, L1 only
Demo design (2026-11-02, with the user): no login or product click-through. Replay a **demo scenario** (the start of a
test-split simulated session) to Kafka under a fresh session id, then type in one search bar with two columns:
Static (L1) and Session-aware (L1 + L2). Only the click sequence is pre-baked; features and rankings are live.
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
- [x] `/api/search` deployed (2026-11-03): app resources `rts-query-embedding` (`CAN_QUERY`) and a `uc_securable`
  `TABLE` / `SELECT` on `products_hybrid_idx` (the index's kind is `TABLE_ONLINE_VECTOR_INDEX_REPLICA`; the SP gets a
  direct SELECT, no grants job needed). Verified as the SP from the browser: HYBRID and ANN both `embedding: custom`,
  200 hits. Single requests, indicative: ANN embed 79 / search 213 ms; HYBRID first request after restart embed 467
  (cold connection) / search 267 ms. Notebook-path search was p50 586 ms, so the app path looks far faster; step 7 confirms
- [ ] UI search results column
- [x] Warm the embedding and QAC L2 clients at startup, in the background: the first search embedded in 78 ms
  (was 467 ms)
- [x] `l2` carries only its status unless reranked (was a duplicate of `l1`: 200 products, ~2x payload)

### Step 5: QAC L2 in the app (model and endpoint: [ml/PLAN.md](../ml/PLAN.md))
- [x] App: `/api/qac` calls L2 with the timeout and falls back to L1 (2026-11-03). `rts-qac-l2` app resource
  (`CAN_QUERY`); L1 top 20 reranked, top 10 shown; status `ok` / `timeout` / `error` (no L2 items unless `ok`; the UI
  shows L1). An unknown or missing session scores fine (same scores as no history), so L2 runs on every keystroke.
  Every L2 call logs its full time (`qac_l2 call ... ms`), including calls that finish after the timeout
  - **Latency from inside the app** (`/api/bench/qac_l2`, 100 back-to-back calls, no timeout):

    | | min | p50 | p95 | p99 |
    |---|---|---|---|---|
    | guest, 1 / 20 candidates | 24.4 / 24.9 | 27.9 / 27.6 | 35.2 / 33.6 | 40.7 / 36.5 |
    | session + user, 1 / 20 | 24.7 / 25.3 | 27.5 / 28.7 | 32.6 / 37.9 | 40.4 / 39.6 |

    A ~25 ms floor per call; candidate count and key lookups add nothing measurable (caveat: the session pipeline was
    stopped, so the session lookup likely missed). The cost is calling an endpoint, not the model or features
  - Timeout 25 → 33 → **36 ms** (the user: the whole request stays within 40 ms): 40 ms QAC budget - L1 p95. 33 ms
    assumed L1 ~7 ms from one sample; `/api/bench/qac_l1` (300 back-to-back lookups, 10 prefixes) measured L1
    p50 2.8 / p95 3.7 / p99 4.8 / max 6.5 ms, matching the notebook (2.9 / 3.7). The timeout enforces the budget; at
    36 ms L2 shows on ~p95 of keystrokes (bench p95 33.6–37.9). Rejected: scoring in the app with direct Lakebase lookups (removes the hop, but splits
    the shared feature definition); keeping 25 ms (L2 almost never shows)
  - Typing-pace calls measured 32–56 ms (3 calls) vs ~28 ms back-to-back; idle connections may cost more. Step 7
    measures at typing pace and reports the fallback rate
- [ ] Streaming pipeline running during Play, so replayed events reach session features (then re-run the bench with a
  live session)
- [ ] Demo: replayed scenarios flip the right column vs L1

### Step 7: Benchmark
- [ ] `07_latency_bench`: per-stage p50/p95, L1-only vs L1+L2
