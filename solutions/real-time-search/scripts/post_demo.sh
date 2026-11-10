#!/usr/bin/env bash
# After a demo: stop what bills while idle. Run from real-time-search/:  scripts/post_demo.sh [config]
# scripts/pre_demo.sh brings it all back (~15 min).
#
# Deletes: the three serving endpoints (they can't be stopped; never leave them on scale-to-zero instead).
# Stops:   the app, the session pipeline, and the Kafka ingestion pipeline.
# Keeps:   the AI Search endpoint `rts-vs-endpoint` (e2-demo-fe is at its 500 vector search endpoint quota, so a
#          deleted one may not come back); Lakebase `rts-online-store` (online store + QAC prefixes; bills 24/7);
#          tables, jobs and the feature definitions.
# Logs: /tmp/rts_post_demo/<step>.log.
set -euo pipefail
cd "$(dirname "$0")/.."
LOG=/tmp/rts_post_demo
source scripts/_common.sh

delete_endpoint() {
  if databricks serving-endpoints get "$1" -p "$P" >/dev/null 2>&1; then
    databricks serving-endpoints delete "$1" -p "$P" && echo "deleted $1"
  else echo "$1: already gone"; fi
}

stop_app() {
  local state
  state=$(databricks apps get "$APP" -p "$P" -o json | json 'd.get("compute_status", {}).get("state", "")')
  echo "compute: $state"
  [ "$state" = "STOPPED" ] || databricks apps stop "$APP" -p "$P"
}

stop_pipeline() {  # stop_pipeline <id-lookup function>
  local pipeline state
  pipeline=$($1)
  state=$(pipeline_state "$pipeline")
  echo "pipeline $pipeline: $state"
  [ "$state" = "IDLE" ] || databricks pipelines stop "$pipeline" -p "$P"
}

echo "Stopping billable demo resources (profile $P)"
for e in $ENDPOINTS; do start "delete_$e" delete_endpoint "$e"; done
start app stop_app
start session_pipeline stop_pipeline session_pipeline_id
start ingestion_pipeline stop_pipeline ingestion_pipeline_id
wait_all
echo "Kept: rts-vs-endpoint (quota), Lakebase rts-online-store. Bring everything back with scripts/pre_demo.sh."
