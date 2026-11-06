#!/usr/bin/env bash
# Before a demo: bring everything back up, move the simulated history to end yesterday, then check it all.
# Run from real-time-search/:  scripts/pre_demo.sh [config]   (default shared/configs/dev.yml; ~15 min)
#
# 1. In parallel: the three serving endpoints (ml bundle; safe to rerun: ensure_endpoint creates them, or updates the
#    model version or scale-to-zero), the AI Search index if it's missing, the app, the session pipeline, and demo_job
#    (time_shift → personas; scenarios).
# 2. App bundle deploy: recreated endpoints lose the app SP's CAN_QUERY until the app's resources are re-applied.
# 3. scripts/pre_demo_checks.py: PASS/FAIL per check; this script's exit code is its result.
# Runs the ml/data jobs as deployed (no ml/data deploy: a worktree behind another track would overwrite its code).
# Step 2 does deploy the app bundle, so run from a checkout whose app/ is what should be live (main or rts-app).
# Logs: /tmp/rts_pre_demo/<step>.log. Stop billable resources after the demo (top-level PLAN.md).
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=/tmp/rts_pre_demo
source scripts/_common.sh

ml_endpoints() {
  cd ml
  local pids=() rc=0
  databricks bundle run qac_l2_job -t dev -p "$P" --only qac_l2_endpoint & pids+=($!)
  databricks bundle run retrieval_job -t dev -p "$P" --only serp_query_embedding & pids+=($!)
  databricks bundle run serp_l2_job -t dev -p "$P" --only serp_l2_endpoint & pids+=($!)
  for pid in "${pids[@]}"; do wait "$pid" || rc=1; done
  return $rc
}

ensure_index() {
  # The workspace's cleanup bot has deleted it before (2026-11-06); serp_l1_ai_search recreates it from
  # products_search (managed embeddings for ~170K products: the slow step when it runs) and checks it.
  local index
  index="$SCHEMA_FQN.$(cfg 'c["ai_search"]["index"]')"
  if databricks vector-search-indexes get-index "$index" -p "$P" >/dev/null 2>&1; then echo "$index exists"
  else echo "$index missing: rebuilding"; cd ml && databricks bundle run retrieval_job -t dev -p "$P" --only serp_l1_ai_search; fi
}

start_app() {
  local state
  state=$(databricks apps get "$APP" -p "$P" -o json | json 'd.get("compute_status", {}).get("state", "")')
  echo "compute: $state"
  [ "$state" = "ACTIVE" ] || databricks apps start "$APP" -p "$P"
}

start_session_pipeline() {
  local pipeline state
  pipeline=$(session_pipeline_id)
  state=$(pipeline_state "$pipeline")
  echo "pipeline $pipeline: $state"
  [ "$state" = "RUNNING" ] || databricks pipelines start-update "$pipeline" -p "$P"
}

demo_data() {
  cd data && databricks bundle run demo_job -t dev -p "$P"
}

app_deploy() {
  cd app && databricks bundle deploy -t dev -p "$P"
}

echo "1/3 Bring up endpoints, app, session pipeline; shift the history (profile $P)"
start ml_endpoints ml_endpoints
start ai_search_index ensure_index
start app start_app
start session_pipeline start_session_pipeline
start demo_data demo_data
wait_all

echo "2/3 Re-apply the app's resources (CAN_QUERY on recreated endpoints)"
start app_deploy app_deploy
wait_all

echo "3/3 Checks"
uv run --no-project -q --with "databricks-sdk>=0.81" --with pyyaml python -I scripts/pre_demo_checks.py "$CONFIG"
