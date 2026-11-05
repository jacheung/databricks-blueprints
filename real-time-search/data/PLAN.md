# Data track: plan and status

Catalog prep, clickstream simulator, Kafka session events, Feature Views and demo scenarios (`data/` bundle). Read the top-level [PLAN.md](../PLAN.md) first (question, rules, shared resources, contracts, decisions).
A box is ticked only when its check has passed on Databricks (`e2-demo-fe`, `main.jon_cheung`) or in `pytest`.
Update this file, plus this track's one status line in the top-level file, at the end of every step.

## Open items
- The session streaming pipeline (`b2e71ddb-8541-4ffa-8c4e-82ecbeca22da`) runs continuously; stop it when not testing.
  Stopped 2026-11-01 (IDLE). Restart with `databricks pipelines start-update <id> -p e2-demo-fe` before freshness checks or the app.
- `kafka_stream` replays history before `feature_views` materializes, so the session online tables hold only events
  produced after the pipeline started. Fine for probes and live traffic; revisit before the app demo (step 4).
- `preflight` records FAIL checks but doesn't assert, so `catalog_job` continues past a failed preflight.
- The full ~97K-query catalog run is still blocked on `ai_classify` throughput (step 1).
- **Batch features go stale as the simulated history ages** (see Gotchas). The last `clickstream_sim` run ended its
  history on 2026-10-30. Item features (7 d) reach zero around 2026-11-06, and user features (30 d) around 2026-11-29.
  Measured 2026-11-05: `user_n_clicks_30d` total 139.5K on 10-30 → 111.3K on 11-05 (−3.4%/day); items with
  features 5,125 → 941 and 23,837 → 5,557 in the two item tables. After those dates the L2 rankers get no user or
  item signal, and the demo's sign-in personas stop changing rankings.

## Gotchas
- Feature deletes: delete materializations before the feature. For batch features, delete the offline one, which
  removes the online one too.
- Don't `DELETE` from a source table under live streams. Rebuilding `session_events` (`clickstream_job` with
  `rebuild_data=true`) breaks them, so run `teardown_job` with `confirm=yes` first.
- Freshness is measured server-side (`commit_time - event_time`, `is_timer = false`), within the 30-minute window.
- A new streaming pipeline reads Kafka from the latest offset and its flows start minutes after creation. `feature_views` now
  waits for a readiness probe to land before measuring, and asserts that every probe landed and p95 < 1 s.
- **Stale batch features.** The simulated history ends the day before the `clickstream_sim` run, but the user (30 d)
  and item (7 d) `SlidingWindow`s slide with the calendar: the cron writes one row per day stamped with the window's
  end. Each day after the sim run, 1/window of the history ages out, so personalization fades with no error anywhere.
  If L2 rankings stop reacting to a signed-in user, or SERP L2 stops helping, check this first:
  `SELECT DATE(event_time), COUNT(*), SUM(user_n_clicks_30d) FROM <rts_user_offline_* holding user_n_clicks_30d> GROUP BY 1 ORDER BY 1`
  (the same per day for `rts_item_offline_*`). Totals falling day by day after the sim's `end_date` mean stale history.
  Fix: rerun `clickstream_job` with fresh dates (`rebuild_data=true`, after `teardown_job` with `confirm=yes`), then the
  demo scenarios and personas.

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

### Step 4: Demo scenarios (the app replays them; see [app/PLAN.md](../app/PLAN.md))
- [x] `demo_scenarios` (data, `demo_job`): 6 scenarios (3 logged-in, 3 guests), synced to `demo_scenarios_online`
- [x] Stricter scenario selection (2026-11-03, rerun green): the old picks repeated one query 4x in 4 of 6 scenarios
  (the simulator draws each query independently with replacement, and ranking by brand clicks favoured repeaters),
  and 3 of 6 next queries were repeats. Now: 3+ distinct earlier searches, none repeated back to back, a next query not
  searched earlier and in the intent category; 688 of 2,849 test candidates pass (20 categories). New picks: Canon,
  Always, Kohler, Nike, Beats, REVLON. Left as is: 2 non-adjacent repeats (sc3, sc6); next queries name the brand only in
  sc4/sc5; brand shoppers click almost only their brand (`BRAND_BOOST`). Simulator realism: PARKING_LOT
- [x] `demo_personas` (2026-11-05, `demo_job`'s second task, checks green): the app's sign-in personas
  (`app.demo_personas`: a name and a category each), picked from the user features' latest values as the most
  concentrated user with 15+ events in the category (`rts_shared.scenarios.pick_personas`); records
  `features_as_of`; synced to `demo_personas_online`. Picks: Ali Ghodsi `u007808` (Electronics, 39 events), Reynold Xin
  `u003564` (Camera & Photo, 29), Matei Zaharia `u018671` (Clothing, Shoes & Jewelry, 25), all 100% in category.
  Scenario labels no longer say guest or logged in: the app replays them under whoever is signed in.
