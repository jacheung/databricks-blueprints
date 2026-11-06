# Sourced by pre_demo.sh and post_demo.sh (run from real-time-search/): config values and resource lookups.
CONFIG="${1:-shared/configs/dev.yml}"
cfg() {  # cfg '<python expression over c>' → value from the config
  uv run --no-project -q --with pyyaml python -I -c "import yaml; c = yaml.safe_load(open('$CONFIG')); print($1)"
}
json() { python3 -c "import json, sys; d = json.load(sys.stdin); print($1)"; }
P=$(cfg 'c["workspace"]["profile"]')
SCHEMA_FQN=$(cfg 'c["unity_catalog"]["catalog"] + "." + c["unity_catalog"]["schema"]')
ENDPOINTS=$(cfg '" ".join(c["models"][m]["endpoint"] for m in ("qac_l2", "query_embedding", "serp_l2"))')
APP=rts-search  # app/resources/app.yml

# Pipeline IDs are looked up, not hard-coded: they change when the features or the Stream are re-created.
session_pipeline_id() {  # Real-Time Mode → Lakebase session tables (the app's Play needs it)
  local mat
  mat=$(databricks feature-engineering list-materialized-features --feature-name "$SCHEMA_FQN.sess_n_clicks_30m" \
        -p "$P" -o json | json 'd[0]["materialized_feature_id"]')
  databricks api get "/api/2.0/feature-engineering/materialized-features/$mat" -p "$P" | json 'd["pipeline_id"]'
}
ingestion_pipeline_id() {  # Kafka topic → session_events_ingest (training history only)
  databricks feature-engineering get-stream "$SCHEMA_FQN.$(cfg 'c["kafka"]["stream"]')" -p "$P" -o json \
    | json 'd["ingestion_config"]["ingestion_pipeline_id"]'
}
pipeline_state() { databricks pipelines get "$1" -p "$P" -o json | json 'd.get("state", "")'; }

NAMES=() PIDS=()
LOG="${LOG:-/tmp/rts_demo}"
mkdir -p "$LOG"
start() { local name=$1; shift; ( "$@" ) >"$LOG/$name.log" 2>&1 & PIDS+=($!); NAMES+=("$name"); }
wait_all() {  # waits for every start()ed step; prints ok/FAIL (+ log tail); nonzero if any failed
  local rc=0 i
  for i in "${!PIDS[@]}"; do
    if wait "${PIDS[$i]}"; then echo "  ok    ${NAMES[$i]}"
    else echo "  FAIL  ${NAMES[$i]} ($LOG/${NAMES[$i]}.log):"; tail -15 "$LOG/${NAMES[$i]}.log" | sed 's/^/        /'; rc=1; fi
  done
  NAMES=() PIDS=()
  return $rc
}
