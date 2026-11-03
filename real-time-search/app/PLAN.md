# App track: plan and status

FastAPI routes, React UI, app resources and grants, and the latency benchmark (`app/` bundle). Read the top-level [PLAN.md](../PLAN.md) first (question, rules, shared resources, contracts, decisions).
A box is ticked only when its check has passed on Databricks (`e2-demo-fe`, `main.jon_cheung`) or in `pytest`.
Update this file, plus this track's one status line in the top-level file, at the end of every step.

## Status
**Status as of 2026-11-03:** services resumed and checked (`check_query_embedding.py` OK, `rts-qac-l2` READY,
`rts-vs-endpoint` ONLINE). `/api/search` deployed and verified as the app SP (below).

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
- [ ] Warm the embedding client at startup (first request paid ~400 ms of connection setup)
- [ ] `l2.items` duplicates `l1` (200 products, ~2x payload): return status only while L2 isn't deployed

### Step 5: QAC L2 in the app (model and endpoint: [ml/PLAN.md](../ml/PLAN.md))
- [ ] App: `/api/qac` calls L2 with the timeout and falls back to L1; serving-endpoint app resource; endpoint-scoped
  OAuth token; the streaming pipeline must run for replayed events to reach features
- [ ] Demo: replayed scenarios flip the right column vs L1

### Step 7: Benchmark
- [ ] `07_latency_bench`: per-stage p50/p95, L1-only vs L1+L2
