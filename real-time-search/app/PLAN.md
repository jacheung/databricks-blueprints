# App track: plan and status

FastAPI routes, React UI, app resources and grants, and the latency benchmark (`app/` bundle). Read the top-level [PLAN.md](../PLAN.md) first (question, rules, shared resources, contracts, decisions).
A box is ticked only when its check has passed on Databricks (`e2-demo-fe`, `main.jon_cheung`) or in `pytest`.
Update this file, plus this track's one status line in the top-level file, at the end of every step.

## Status
**Status as of 2026-11-03 (late evening):** QAC L1 + L2 demo deployed with the "Under the hood" panel (clickstream →
features and keystroke → completions), intent timeline, L2 rank badges, brand colors and the L2 inputs bar, all
checked in the browser by the user. The user sees L2 move rows vs L1 after Play (rank badges, NEW). The per-feature
table below the columns is gone (the inputs bar replaces it). Session pipeline **running**; it was stopped at 22:40
UTC, which made every Play time out with "landed: none" until it was restarted (23:04) and caught up (~4 min).

## Next
1. Re-check the L2 timeout share on CPU Medium at the 20 ms debounce (Small: 6/18 calls over 36 ms vs 1/8 at 80 ms).
2. Set an end-to-end target (keystroke → on screen p95) from the browser timings; write it next to the 40 ms server budget.
3. User history (open item below): score a logged-in scenario's next query with and without `user_id`.
4. UI search results column (`/api/search`, HYBRID default).
5. Step 7: per-stage p50/p95 at typing pace and the L2 fallback rate; HYBRID vs ANN.

## Open items
- User history in the demo: half the scenarios are logged in and the simulator reuses a user's long-term intent in 70%
  of their intent sessions, but scenarios aren't picked so user history matters, and nothing isolates its effect
  (logged-in lift +0.068 vs guest +0.045 MRR is confounded). Check first: score a logged-in scenario's next query
  with and without `user_id`; show user features only if the ranking changes.
- `sess_last_event_type` skew (cross-track ask, data/ml): online it is always the last click or cart, though the spec
  defines it over all events, so training may have seen `search` values serving never does.
- Check the app's Lakebase SELECT survives a snapshot re-sync (synced tables may be recreated; default privileges are set).
- Pay-per-token fallback in `/api/search` (when `rts-query-embedding` fails): kept for now, visible as
  `embedding: fallback_managed`. The user's call whether to fail instead.

## Checklist

### Step 4: App, L1 only
Demo design (2026-11-02, with the user): no login or product click-through. Replay a **demo scenario** (the start of a
test-split simulated session) to Kafka under a fresh session id, then type in one search bar with two columns:
Static (L1) and Session-aware (L1 + L2). Only the click sequence is pre-baked; features and rankings are live.
- [x] `rts_shared.scenarios.replay`, `rts_shared.lakebase` (sync + pg8000 connect), prefix lookup moved into
  `rts_shared.retrieval`; QAC notebook refactored onto them and rerun green (first attempt); 84 tests pass
- [x] `app/` bundle `rts-app`: FastAPI (`/api/qac`, `/api/scenarios`, `/api/scenarios/{id}/play`) + React two-column UI;
  app `rts-search` running; psycopg[binary] + pool loads fine in the Apps container; `grants_job` gives its SP SELECT
- [x] Verified in the browser (2026-11-03): scenarios list (`GET /api/scenarios` 200), QAC columns, Play `sc1` produced
  to Kafka (`POST .../play` 200; `play_scenario` raises unless every event is flushed, so the SP's MSK credential
  access works). Session `demo-3c7d8c132e`, user `u017508`
- [x] Session panel: read the session's online features back after Play (done as the per-feature table in step 5;
  tables found in Postgres database `main`)
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
    36 ms L2 shows on ~p95 of keystrokes (bench p95 33.6–37.9). Rejected: scoring in the app with direct Lakebase
    lookups (removes the hop, but splits the shared feature definition); keeping 25 ms (L2 almost never shows)
  - Typing-pace calls measured 32–56 ms (3 calls) vs ~28 ms back-to-back; idle connections may cost more. Step 7
    measures at typing pace and reports the fallback rate
- [x] Streaming pipeline running during Play, so replayed events reach session features (2026-11-03: all 3 tables land,
  timed in the panel)
- [x] `/api/bench/qac_l2` with a live session (2026-11-04; `demo-73da6d226d` / `u003858`, a row in all 7 session and
  user tables). p50 / p95 ms, 1 → 20 candidates: miss back-to-back 28.0/34.5 → 28.6/34.8; hit back-to-back 26.7/30.8 →
  28.7/34.8; miss at a 300 ms gap (`gap_ms`, n=30) 28.4/31.8 → 29.9/35.1; hit at 300 ms gap 28.7/32.7 → 30.1/36.7.
  Hits cost nothing extra, a 300 ms idle gap ~1 ms, 20 candidates ~1-2 ms: the cost is a fixed per-call floor (min
  23.5 ms), so p95 ~35-37 ms sits at the 36 ms timeout and ~1 in 20 keystrokes falls back by design. Unknown: whether a
  miss still runs the lookups (if so, this measures only the cost of finding rows)
- [x] Keystroke → on screen bar (2026-11-04, deployed; numbers read by the user, 10+ queries, stable): browser-timed
  from the keystroke event to the painted frame: debounce 81.9, network + app 53.4 (round trip minus `timings_ms`),
  server 34.3, render 6.3 = 175.9 ms. The server is a fifth of it. Only keystroke-triggered requests are timed
- [x] Debounce 80 → 20 ms (2026-11-04; the user: "feels super snappy"). The 80 ms debounce dropped half the requests
  for a fast typist ("wireless earbuds": 8 requests for 16 keys; at 20 ms one per key). On CPU Small the extra
  requests queued: 6/18 L2 calls over 36 ms (max 103) vs 1/8 (logs; small samples, part of the 20 ms run rolled out of
  the log window), so `rts-qac-l2` went to CPU Medium; it was also found with scale-to-zero on, now off. The ml
  track's `ensure_endpoint` applies config only on a new model version, so size/scale changes reach an existing
  endpoint only by recreation or a manual `update-config`
- [ ] Demo: replayed scenarios flip the right column vs L1 (2026-11-03: the user sees rank badges and NEW rows after
  Play; not yet checked across scenarios)
- [x] Latency and freshness bars (2026-11-03; freshness checked in the browser by the user with the pipeline on). QAC: a
  stacked L1 + L2 bar, dashed marker at `budgets_ms.qac_total_p95` (40 ms), fixed 1.5x scale; server-side times only.
  On a timeout the UI fetches the call's full time (`/api/qac/{request_id}/l2_time`, `RecentTimes`, 1 test) and draws
  the overrun hatched past the budget, with "N ms over budget". Overrun drawing not yet checked in the browser.
  Clickstream: after Play the UI polls `/api/sessions/{id}/freshness` (250 ms, gives up after 15 s) until the
  session's online row holds every replayed search, click and cart, then shows Kafka ack (app-side, produce → all
  acked) and stream → online store, on a 1 s scale. The lag is the row's `commit_time - event_time` (the data track's
  freshness measure; `session_freshness`, 3 tests, 121 pass), so the poll interval doesn't change it. The online table
  (`rts_session_online_<suffix>`) is looked up by its `sess_n_clicks_30m` column, since re-materializing renames it.
  Found via `pg_catalog` (`information_schema` hides tables the role has no privilege on); the app SP reads it
- [x] End-to-end bars + session features (2026-11-03; checked in the browser by the user), modelled on the NBO
  accelerator app. All in the side panel "Under the hood" (the user, 2026-11-03: less text under the search bar):
  first "Clickstream → features" (Kafka write, app; Real-Time Mode, `output_time`; Lakebase write, `commit_time`; app
  read) and "Keystroke → completions" (L1, L2, overrun), each a bar with one row per segment on one ms scale (the
  40 ms budget line on the keystroke bar only), then planted intent and clickstream history. Keystroke axis fixed at
  60 ms (the user: covers p99 end to end; slower ones clip with ▸); the clickstream bar fits its own total. Both bars and all their
  rows render from page load with "–" and fill in on Play / the first keystroke, so the layout never shifts. (A collapsed
  per-feature table below the columns was removed 2026-11-03; the L2 inputs bar replaces it.) `/api/sessions/{id}/freshness` waits server-side, reading the 3 session tables every 10 ms until
  each matches what Play sent (`expected_session_features`, `landed`, `freshness_breakdown`). Timing one event, like
  the NBO app: the replay ends on its last click or cart (the only event that moves all 3 tables); Play sends the rest
  untimed, then stamps and sends that click alone, so "Kafka write" is one event's ack. Before, it was the whole
  replay's (55 ms) while the pipeline had output the row 30 ms in, so Real-Time Mode came out -25 ms. A table counts
  as landed only when its row carries that event's event_time and was committed after it. Stages end at timestamps,
  add up to the total, and are clamped at 0 (app vs pipeline clock); categories the timed click didn't touch show
  "earlier event". Offsets are from the timed click; earlier events keep simulated past event_times. No "Before" column (Play starts a fresh
  session). The online tables are in the Postgres database named after the catalog (`main`), not
  `lakebase.database` (synced tables only): FE syncs each catalog's online tables into a database of that name (also
  noted in the NBO repo's `pipelineTimes.ts`). The app has a second pool for it (`features_database`, 1 test);
  `grants_job` now grants CONNECT/USAGE/SELECT in both databases and checks all 3 session tables (green 2026-11-03)
- [x] Databricks brand colors (2026-11-03; checked in the browser by the user): CSS tokens from the extended brand guide (navy/oat/white
  surfaces, lava accents; L1 navy, L2 lava, timeout yellow, overrun maroon hatch); text pairs checked >= 4.5:1
- [x] Intent timeline (2026-11-03, deployed; checked in the browser by the user): Play returns `timeline` (the replayed events, in
  seconds before the last one; `scenario_timeline`, 1 test, 118 pass) and the scenario's planted `intent`. The UI shows
  the intent first ("expect the session-aware column to lift completions that match it"), then the next query and
  the events, newest first, in a left side column (below the page under 1500 px wide) so the search bar never moves.
  Rows in both columns whose top category or most-clicked brand match the intent get a tag
- [x] L2 diff in the UI (2026-11-03, deployed; checked in the browser by the user): each L2 item carries `l1_rank` (its position
  in L1 after filtering, not the table `rank`; 2 tests, 117 pass). The Session-aware column shows `▲n` / `▼n` /
  `NEW` (from below L1's shown top 10) per row, tints rows moved up or new, dims unchanged rows, and a summary
  line ("L2 moved 4 of 10, brought in 2 from below L1's top 10"). Only when `l2.status` is `ok`
- [x] L2 inputs bar (2026-11-03, deployed; checked in the browser by the user): the Session-aware subtitle reads "L1 + L2:
  with features from" plus a bar of what the QAC L2 endpoint receives, widths by count: 12 streaming (session,
  Kafka → Real-Time Mode), 4 batch (user, hourly Delta), 13 request (the L1 row + `is_guest`). Groups come from
  `rts_shared` (`l2_inputs`, 1 test), so they follow the endpoint; item features aren't QAC L2 inputs. Hover or focus a
  segment: its first 3 features with value and when the app read them, then "… N more". Streaming values and `+N ms`
  come from the freshness check; batch from `/api/users/{user_id}/features` (the 4 `rts_user_online_*` tables, found
  by column like the session ones; `user_feature_values`, 1 test; read once per Play), shown as "hourly batch" since
  those tables carry no timestamp; request values are exactly what was sent for the top completion. Guests get a
  hatched batch segment. These are the app's own reads: the endpoint returns only scores
- [x] Column headers aligned (2026-11-03, deployed; not yet checked in the browser): both columns share one header row
  (CSS subgrid; note and diff summary moved into `<header>`), so the lists start level whatever Session-aware shows

### Step 7: Benchmark
- [ ] `07_latency_bench`: per-stage p50/p95, L1-only vs L1+L2. Started: `/api/bench/qac_l1` and `/api/bench/qac_l2`
  (back-to-back, from inside the app); `gap_ms` adds typing pace (idle before each call, untimed; 1 test). Still
  needed: fallback rate, SERP embed + search
