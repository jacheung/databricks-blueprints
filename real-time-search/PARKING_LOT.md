# Parking Lot

Items deliberately deferred, not overlooked. Scope stays at QAC + SERP. Check here before starting new work on this project.

---

## 1 — Zero-query (QAC with an empty prefix)

**What:** Suggestions when the search box is focused but empty. This is where clickstream matters most, since there's no prefix to go on. There are three cases:
- **First session of a new or guest user:** trending and popular queries. This is the only case with no context to use.
- **Returning user opens the search box:** recent searches, plus suggestions personalized from long-term user features (`user_cat_*_30d`, `user_median_price_30d`). These are user-level features, so nothing from the current session leaks in.
- **Mid-session, the user clears the box:** session intent is exactly the signal to use. Someone who just viewed a $2,000 lens should see "camera bags". That's the point, not pollution.

**Why deferred:** It costs almost nothing to add later because it's QAC with `prefix = ''`, and it needs no new infrastructure:
- a `prefix = ''` row in `qac_prefix_completions` holding trending queries;
- the user's recent searches from `session_events`;
- the same QAC L2 reranker and features.

**Entry points:** the ML bundle's QAC L1 notebook (emit the empty-prefix row), the app's `/qac` handler (allow an empty prefix, merge in recent searches), and the QAC L2 training notebook (empty-prefix impressions in training).

---

## 2 — Search-as-you-type (product tiles in the typeahead)

**What:** Show products, not just phrases, while the user types.

**Why deferred:** Full search-as-you-type is SERP at QAC latency. It multiplies AI Search and L2 QPS by the number of keystrokes per query, roughly 5–10x. Relevance on partial queries is also poor, and it tends to cannibalize SERP.

**Cheaper middle ground, if RCT customers ask:** show 3–4 product tiles in the typeahead dropdown. Fire SERP L1 only for QAC's top completion, with a small k, and cache the result. Skip L2 or use a light model. This reuses everything already built, so it's an add-on, not a design change.

**Entry points:** the app's `/qac` handler (L1 call for the top completion, with a cache) and `ai_search.serp_l1_k` (a separate small k for tiles).

---

## 3 — ~~Kafka `StreamSource` for ~200ms feature freshness~~ (moved into scope, 2026-10-29)

**Status:** Kafka is now the session-event path (`data/notebooks/kafka_stream.py`). The Delta path (`DeltaTableSource` +
`StreamingMode` over a CDF table) measured **p50 158 s / p95 186 s** event → online across 18 probes (server-side
`commit_time - event_time`), with 0 under 1 s. That is too slow for in-session personalization, so session
features read a Kafka `StreamSource`. User and item features stay on Delta + cron, where freshness in hours is fine.

**Still open:** whether the app writes events straight to Kafka, or to Zerobus with a Kafka copy for analytics.

---

## 4 — Sequence features (SASRec)

**What:** Use the ordered last N events (`Search → Click → Back → Add to Cart`), not just aggregates and the latest value.

**Why deferred:** Feature Views give aggregates and the last value. `SawtoothWindow` + `LastN` may give an ordered last-N list; check whether it works with `DeltaTableSource` streaming. If it doesn't, this needs a custom Lakebase state table or a session embedding.

---

## 5 — Catalog prep as a Lakeflow Declarative Pipeline

**What:** Rebuild `data/notebooks/catalog_prep.py` (ESCI download, `ai_classify`, products/queries/labels) as a
Lakeflow Spark Declarative Pipeline instead of a notebook job.

**Why deferred:** The notebook already resumes `ai_classify` from checkpoints, and it runs rarely. Moving it buys
lineage and expectations but doesn't change any measured number. Keep notebook jobs until the blueprint answers its
question.

**Entry points:** `data/resources/catalog.job.yml` becomes a pipeline resource; `rts_data.datagen` helpers stay.

---

## 6 — Smaller follow-ups

- **Treelite:** compile the QAC LightGBM model if inference time becomes a meaningful share of the 40ms budget.
- **Cross-encoder SERP L2:** a fine-tuned small transformer over (query, title) pairs plus session features. This is where a GPU is clearly justified.
- **Business-logic blend:** margin and inventory boosts applied after L2 in the app, not in the model.
- **Dynamic facets** on the SERP.
- **SERP index text variants:** compare recall@200 for title + brand (current) vs + category, and gte vs qwen3-embedding (the latter wants a query-side instruction prefix that managed embeddings don't add). Deferred 2026-11-02 to get end-to-end first.

---

## 7 — Realistic search sequences in the clickstream simulator

**What:** `rts_data.datagen` draws each search's query independently, with replacement, from the intent's popularity-weighted pool. For niche brands that pool is a few queries, so sessions often repeat one query (4 of 6 old demo scenarios searched the same query 4x). Real sessions refine (broad → brand → specific), rarely repeat back to back, and brand shoppers still click other brands. Fix: draw without replacement within a session, add a broad → brand → specific path, soften `BRAND_BOOST`, and rank `TOP_BRANDS` by demand instead of product count.

**Why deferred:** it regenerates the clickstream, so features, the QAC table, QAC L2 training and evaluation all rerun (data + ml tracks). The demo was fixed by stricter scenario selection instead (data/PLAN.md, 2026-11-03).

**Entry points:** `data/src/rts_data/datagen.py` (`simulate`, `Catalog.build`), its tests, then the clickstream job and the ml track's QAC notebooks.

