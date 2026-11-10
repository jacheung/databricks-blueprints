# Recommender System - Cart Checkout Demo

An end-to-end recommendation engine built on Databricks that serves personalised suggestions to **logged-in users** via collaborative filtering and **guest users** via market basket analysis.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────────┐
│                          Serving Layer                               │
│                                                                      │
│   Logged-in user ──► ALSRecommenderModel (PyFunc)                    │
│                      └── load_context: reads pre-computed recs       │
│                          from Lakebase online table                  │
│                      └── predict(mpid) → ranked recommendations      │
│                                                                      │
│   Guest user ──────► MBARecommenderModel (PyFunc)                    │
│                      └── load_context: reads association rules       │
│                          from Lakebase online table                  │
│                      └── predict(cart) → cross-join × rules → top-k  │
└─────────────────────────────────────────────────────────────────────┘
         ▲                                       ▲
         │                                       │
    02_collaborative_filter               01_market_basket_analysis
         │                                       │
         └──────────── 00_data_preparation ──────┘
```

## Why Two Models?

| Scenario | Model | Rationale |
|---|---|---|
| **Logged-in user** | ALS Collaborative Filter | We know the user's history — ALS leverages past orders to surface items similar users also enjoy |
| **Guest / anonymous** | Market Basket Analysis | No user history, but we have their current cart — MBA recommends items frequently purchased *together* with what's already in the cart |

In production the application checks whether a `user_id` (mpid) is available. If yes, it hits the ALS serving endpoint for instant pre-computed recommendations. If not, it sends the current cart to the MBA serving endpoint.

---

## Notebooks

### `00_data_preparation`
Generates a synthetic dataset of **15,000 historical cart transactions** across 2,000 users for a pizza chain. Performs basic data cleansing (removes sauces and utensils that should never be recommended) and saves five tables:

| Table | Purpose |
|---|---|
| `cleaned_mapped_dataset` | Full cleaned order history |
| `train_dataset` | 80% training split (seed=42) — used by all downstream notebooks |
| `test_dataset` | 20% held-out test split — shared across notebooks for fair Hit@k comparison |
| `item_catalog` | Item-level features: category, base price, calories bucket, vegetarian flag |
| `user_features` | User-level features: order history stats, preferred category, customer tier, recency |

### `01_market_basket_analysis`
Trains an FPGrowth association rules model on `train_dataset` and evaluates on the **shared test set**.

**Sections:**
1. **Support Calculation** — item frequency analysis and visualisation (training data only)
2. **Model Construction** — FPGrowth training on `train_dataset`, Hit@k evaluation on `test_dataset` vs. popularity baseline
3. **Deployment** — two serving patterns:
   * **3a — Packaged Parquet** (`MBARecommenderModel`) — rules bundled as a static MLflow artifact (simple, self-contained)
   * **3b — Lakebase-backed PyFunc** (`MBALakebaseRecommenderModel`) — rules read from Lakebase at load time (for large rule sets or always-fresh rules)

### `02_collaborative_filter`
Trains a PySpark ALS model with implicit feedback (proportion-of-orders rating).

**Sections:**
1. **Data Preparation** — loads `train_dataset` and `test_dataset`, splits training data further into `train_hp` (80%) / `val_hp` (20%) for HPO, builds user/item integer mappings
2. **Model Construction** — baseline ALS evaluated on `val_hp`, Optuna HPO (20 trials) on `train_hp`/`val_hp`, final model trained on all training data, final Hit@k on shared `test_dataset`
3. **Deployment** — three steps:
   * **3a — Pre-compute** — top-20 recommendations per user, reverse-mapped to product slugs, saved as `als_recommendations` Delta table
   * **3b — Lakebase publish** — enables CDF, adds primary key, syncs to Lakebase for point-lookup serving
   * **3c — PyFunc model serving** (`ALSRecommenderModel`) — Lakebase-backed PyFunc logged to MLflow / Unity Catalog

---

## Data Flow

```
Synthetic orders (00)
  │
  ├──► cleaned_mapped_dataset
  ├──► item_catalog  (item features — used by notebook 03)
  ├──► user_features (user features — used by notebook 03)
  │
  ├──► train_dataset
  │       │
  │       ├──► 01_MBA ──► association_rules ──► MBARecommenderModel (Model Serving)
  │       │                                └──► Lakebase synced table
  │       │
  │       └──► 02_ALS ──► als_recommendations ──► ALSRecommenderModel (Model Serving)
  │                                          └──► Lakebase synced table
  │
  └──► test_dataset (shared evaluation — both notebooks)
```

## Deployment Summary

| Model | PyFunc Class | Serving Mechanism | Input | Latency |
|---|---|---|---|---|
| ALS (logged-in) | `ALSRecommenderModel` | Lakebase synced table lookup | `mpid` | O(1) per user |
| MBA (guest) | `MBARecommenderModel` | Lakebase-backed cross-join | `cart` (list of items) | Proportional to \|rules\| × \|cart\| |

Both models are logged to MLflow / Unity Catalog and can be deployed to a Model Serving endpoint. The frontend calls a single endpoint per model — no need to know about Lakebase, ALS, or integer mappings.

---

## Quick Start

1. **Run `00_data_preparation`** — generates synthetic data and feature tables, saves all outputs to Unity Catalog
2. **Run `01_market_basket_analysis`** — trains FPGrowth, evaluates on shared test set, logs PyFunc to Unity Catalog
3. **Run `02_collaborative_filter`** — trains ALS with HPO, evaluates on shared test set, publishes recommendations to Lakebase

All notebooks use `catalog = 'users'` and `schema = 'jon_cheung'` by default — update the configuration cell (cell 2) in each notebook to point to your own catalog and schema.

## Configuration

Each notebook has a configuration cell (cell 2) that defines all user-configurable names. No table names, model names, or project IDs are hardcoded anywhere else.

### `00_data_preparation`

| Parameter | Default | Description |
|---|---|---|
| `catalog` | `users` | Unity Catalog catalog |
| `schema` | `jon_cheung` | Unity Catalog schema |
| `cleaned_table` | `users.jon_cheung.cleaned_mapped_dataset` | Full cleaned dataset |
| `train_table` | `users.jon_cheung.train_dataset` | Training split |
| `test_table` | `users.jon_cheung.test_dataset` | Test split |
| `item_cat_table` | `users.jon_cheung.item_catalog` | Item features |
| `user_feat_table` | `users.jon_cheung.user_features` | User features |

### `01_market_basket_analysis`

| Parameter | Default | Description |
|---|---|---|
| `catalog` / `schema` | `users` / `jon_cheung` | Unity Catalog location |
| `train_table` / `test_table` | `...train_dataset` / `...test_dataset` | Input tables |
| `experiment_name` | `/Users/.../MBA_recommender_model` | MLflow experiment path |
| `model_name` | `users.jon_cheung.mba_recommender_model` | Packaged parquet PyFunc |
| `model_name_lakebase` | `users.jon_cheung.mba_recommender_lakebase` | Lakebase-backed PyFunc |
| `rules_table` | `users.jon_cheung.association_rules` | FPGrowth output table |
| `lakebase_project_id` | `pizza-chain-recommender` | Shared Lakebase project (notebooks 01–03) |
| `postgres_database` | `databricks_postgres` | Default database created with the project |

### `02_collaborative_filter`

| Parameter | Default | Description |
|---|---|---|
| `catalog` / `schema` | `users` / `jon_cheung` | Unity Catalog location |
| `train_table` / `test_table` | `...train_dataset` / `...test_dataset` | Input tables |
| `experiment_name` | `/Users/.../ALS_recommender_model` | MLflow experiment path |
| `model_name` | `users.jon_cheung.als_recommender_model` | Lakebase-backed PyFunc |
| `recs_table` | `users.jon_cheung.als_recommendations` | Pre-computed recommendations |
| `lakebase_project_id` | `pizza-chain-recommender` | Shared Lakebase project (notebooks 01–03) |
| `postgres_database` | `databricks_postgres` | Default database created with the project |

## Requirements

| | |
|---|---|
| **Runtime** | Databricks Runtime 17.3 ML |
| **Compute** | Classic cluster (notebooks 00–02) |
| **Packages** | `optuna`, `databricks-sdk` — installed in-notebook via `%pip install` |
