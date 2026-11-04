# App track: plan and status

FastAPI routes, React UI, app resources and grants, and the latency benchmark (`app/` bundle). Read the top-level [PLAN.md](../PLAN.md) first (question, rules, shared resources, contracts, decisions).
A box is ticked only when its check has passed on Databricks (`e2-demo-fe`, `main.jon_cheung`) or in `pytest`.
Update this file, plus this track's one status line in the top-level file, at the end of every step.

## Status
**Status as of 2026-11-04:** QAC L1 + L2 demo deployed: Under-the-hood panel (clickstream → features, keystroke →
on screen), intent timeline, L2 rank badges and the L2 inputs bar, checked in the browser by
the user. L2 moves rows vs L1 after Play. Debounce 20 ms; `rts-qac-l2` on CPU Medium: warm, 6% of L2 calls miss the
36 ms timeout (by ≤ 4 ms), but the first seconds after ~3–4 min idle run 70–170 ms, so warm it with a few queries
before presenting. Session pipeline **running** (a stopped one makes Play time out with "landed: none").
QAC moved into a dropdown under the search box (2026-11-04, checked in the browser by the user), freeing the area
below for SERP.

## Next
1. SERP results below the search box (`/api/search`, HYBRID default): Hybrid L1 | L1 + L2 columns ("not deployed"
   until SERP L2 is wired), embed/search timings in the header. With it, Enter or a picked completion searches, and
   "Try …" fills the box, opens the dropdown, then Enter searches. SERP metrics in the Under-the-hood panel later.
2. Step 7: per-stage p50/p95 at typing pace and the L2 fallback rate; HYBRID vs ANN.
Deferred (the user, 2026-11-04): the user-history check (open item below).

## Open items
- User history in the demo: half the scenarios are logged in and the simulator reuses a user's long-term intent in 70%
  of their intent sessions, but scenarios aren't picked so user history matters, and nothing isolates its effect
  (logged-in lift +0.068 vs guest +0.045 MRR is confounded). Check first: score a logged-in scenario's next query
  with and without `user_id`; show user features only if the ranking changes.
- L2 cold start after idle: the first L2 calls after ~3–4 min without traffic take 70–170 ms (fall back to L1), then
  settle within seconds. The user warms it by hand before a demo; a background keep-warm call (1 candidate every
  ~10 s, also `rts-query-embedding`) is the fix if that's not enough. Not known: app connection or endpoint side.
- Cross-track asks (ml/data):
  - `sess_last_event_type` skew: online it is always the last click or cart, though the spec defines it over all
    events, so training may have seen `search` values serving never does.
  - `ensure_endpoint` applies config only on a new model version, so size or scale-to-zero changes reach an existing
    endpoint only by recreation or a manual `update-config` (`rts-qac-l2` was found with scale-to-zero on, 2026-11-04).
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
- [x] Session features read back after Play (tables found in Postgres database `main`); shown first as a per-feature
  table, now in the L2 inputs bar (step 5)
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
- [x] QAC dropdown (2026-11-04, checked in the browser by the user): the Static and Session-aware columns float under
  the search box (same shared header, badges, inputs bar and L2 status). Opens on focus or typing; closes on Esc or
  when focus leaves the box. ↑/↓ + Enter move through Session-aware; a click picks from either column. A pick only
  fills the box for now (not timed as a keystroke); "Try …" focuses the box so the dropdown opens
- [ ] UI search results column (below the search box, freed by the dropdown)
- [x] Warm the embedding and QAC L2 clients at startup, in the background: the first search embedded in 78 ms
  (was 467 ms)
- [x] `l2` carries only its status unless reranked (was a duplicate of `l1`: 200 products, ~2x payload)

### Step 5: QAC L2 in the app (model and endpoint: [ml/PLAN.md](../ml/PLAN.md))

**L2 call and features**
- [x] `/api/qac` calls L2 with the timeout and falls back to L1 (2026-11-03). `rts-qac-l2` app resource
  (`CAN_QUERY`); L1 top 20 reranked, top 10 shown; status `ok` / `timeout` / `error` (no L2 items unless `ok`; the UI
  shows L1). An unknown or missing session scores fine (same scores as no history), so L2 runs on every keystroke.
  Every L2 call logs its full time (`qac_l2 call ... ms`), including calls that finish after the timeout
- [x] Streaming pipeline running during Play, so replayed events reach session features (2026-11-03: all 3 tables
  land, timed in the panel). Its warm-up after a start is ~4 min (~70 s to RUNNING, then the backlog)
- [ ] Demo: replayed scenarios flip the right column vs L1 (2026-11-03: the user sees rank badges and NEW rows after
  Play; not yet checked across scenarios)

**Latency** (server budget: 40 ms p95 for L1 + L2, measured in the app)
- [x] Per-call floor (`/api/bench/qac_l2`, 100 back-to-back calls, no timeout; 2026-11-04 with a live session,
  `demo-73da6d226d` / `u003858`, a row in all 7 session and user tables). p50 / p95 ms, 1 → 20 candidates:

  | | 1 candidate | 20 candidates |
  |---|---|---|
  | miss, back-to-back | 28.0 / 34.5 | 28.6 / 34.8 |
  | hit, back-to-back | 26.7 / 30.8 | 28.7 / 34.8 |
  | miss, 300 ms gap (`gap_ms`, n=30) | 28.4 / 31.8 | 29.9 / 35.1 |
  | hit, 300 ms gap (n=30) | 28.7 / 32.7 | 30.1 / 36.7 |

  Hits, a 300 ms idle gap and 20 candidates each add ≤ 2 ms: the cost is a fixed per-call floor (min 23.5 ms), the
  endpoint call itself, not the model or features. Unknown: whether a miss still runs the lookups
- [x] Timeout 25 → 33 → **36 ms** (the user: the whole request stays within 40 ms): 40 ms budget - L1 p95. L1
  (`/api/bench/qac_l1`, 300 back-to-back lookups, 10 prefixes) p50 2.8 / p95 3.7 / p99 4.8 / max 6.5 ms, matching the
  notebook (2.9 / 3.7). With p95 at the floor's 35–37 ms, ~1 in 20 keystrokes falls back by design. Rejected: scoring
  in the app with direct Lakebase lookups (removes the hop, but splits the shared feature definition); keeping 25 ms
  (L2 almost never shows)
- [x] Keystroke → on screen (2026-11-04, browser-timed from the keystroke event to the painted frame; read by the user,
  10+ queries, stable): at an 80 ms debounce, debounce 81.9 + network and app 53.4 (round trip minus `timings_ms`) +
  server 34.3 + render 6.3 = 175.9 ms. The server is a fifth of it. Only keystroke-triggered requests are timed
- [x] Debounce 80 → **20 ms** (2026-11-04; the user: "feels super snappy"). 80 ms dropped half a fast typist's
  requests ("wireless earbuds": 8 requests for 16 keys; at 20 ms one per key), so per-keystroke QPS sizes the endpoint.
  At 20 ms, keystroke → on screen reads ~120 ± 10–20 ms (the user; one reading: debounce 21.2 + network and app 54.1
  + server 35.9 + render 9.1 = 120.3): the 120 ms target is met typically, not at p95. Kept at 20 ms (the user: good
  enough); debounce 0 (~20 ms less) was the option not taken
- [x] `rts-qac-l2` CPU Small → **Medium**, scale-to-zero (found on) → off (2026-11-04; top-level decision log). From the
  app logs, L2 calls over the 36 ms timeout at a 20 ms debounce:

  | | calls | median | max | over 36 ms |
  |---|---|---|---|---|
  | Small (80 ms debounce) | 8 | 31.0 | 48.7 | 1 (13%) |
  | Small | 18 | 33.5 | 102.6 | 6 (33%) |
  | Medium, warm | 86 | 31.5 | 39.5 | 5 (6%), all ≤ 39.5 ms |
  | Medium, first 3 s after > 60 s idle | 21 | 77.5 | 174.4 | 17 (81%) |

  Small queued at one call per keystroke; Medium doesn't. The idle cold start is separate (open items). Small samples;
  part of the Small 20 ms run rolled out of the log window

**UI**
- [x] Under the hood side panel (2026-11-03; checked in the browser by the user; modelled on the NBO accelerator app;
  the user: less text under the search bar). Bars with one row per segment, rendered from page load with "–" so
  the layout never shifts:
  - "Clickstream → features": Kafka write (app), Real-Time Mode (`output_time`), Lakebase write (`commit_time`), app
    read; fits its own total. After Play the UI asks `/api/sessions/{id}/freshness` once; the server reads the 3
    session tables every 10 ms (up to 15 s) until each matches what Play sent (`expected_session_features`, `landed`,
    `freshness_breakdown`). One event is timed: the replay ends on its last click or cart (the only event that moves
    all 3 tables), sent alone after the rest, so "Kafka write" is one event's ack (the whole replay's was 55 ms while
    the pipeline output the row 30 ms in: Real-Time Mode came out -25 ms). A table counts as landed only when its row
    carries that event's event_time and was committed after it. Stages end at timestamps, add up to the total, and
    are clamped at 0 (app vs pipeline clock); categories the timed click didn't touch show "earlier event". No
    "Before" column: Play starts a fresh session
  - "Keystroke → on screen" (2026-11-04, checked in the browser by the user; replaced a separate "Keystroke →
    completions" bar): debounce, network + app, L1, L2, render. L2 is red when it reranks, red-hatched when it
    didn't (timeout or error). The overrun after a timeout isn't waited for (L1 order already shown), so it's text on
    the L2 row: "finished at N ms" (fetched from `/api/qac/{request_id}/l2_time`, `RecentValues`, 1 test). Axis fixed
    at 160 ms (slower ones clip with ▸). No target line or summary footer (the user)
  - Planted intent and the replayed clickstream, newest first
  - Online tables: in the Postgres database named after the catalog (`main`), not `lakebase.database` (synced tables
    only); the app has a second pool for it (`features_database`, 1 test). Found by a column only they hold
    (`_online_tables`: session by `sess_n_clicks_30m` etc., user by feature name), since re-materializing renames them;
    via `pg_catalog`, as `information_schema` hides tables the role can't read. `grants_job` grants
    CONNECT/USAGE/SELECT in both databases (green 2026-11-03)
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
  come from the freshness check; batch from `/api/users/{user_id}/features` (the 4 `rts_user_online_*` tables;
  `user_feature_values`, 1 test; read once per Play), shown as "hourly batch" since those tables carry no timestamp;
  request values are exactly what was sent for the top completion. Guests get a hatched batch segment. These are
  the app's own reads: the endpoint returns only scores. Replaced the per-feature table below the columns
- [x] Column headers aligned (2026-11-03, deployed; not yet checked in the browser): both columns share one header row
  (CSS subgrid; note and diff summary moved into `<header>`), so the lists start level whatever Session-aware shows

### Step 7: Benchmark
- [ ] `07_latency_bench`: per-stage p50/p95, L1-only vs L1+L2. Started: `/api/bench/qac_l1` and `/api/bench/qac_l2`
  (from inside the app; `gap_ms` idles before each call, untimed, 1 test). Still needed: the fallback rate at typing
  pace (the app logs give it), SERP embed + search
