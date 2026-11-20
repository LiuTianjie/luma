#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT_DIR"

TMP_DIR="$(mktemp -d)"
CH_NAME="luma-eye-e2e-ch-$$"
CH_PORT="${LUMA_EYE_E2E_CH_PORT:-18123}"
CORE_PORT="${LUMA_EYE_E2E_CORE_PORT:-18091}"
EDGE_PORT="${LUMA_EYE_E2E_EDGE_PORT:-18092}"
ADMIN_TOK="e2e-admin-secret-123"
RELAY_TOK="e2e-relay-secret-456"

CORE_PID=""
EDGE_PID=""

cleanup() {
  set +e
  [[ -n "$EDGE_PID" ]] && kill "$EDGE_PID" 2>/dev/null || true
  [[ -n "$CORE_PID" ]] && kill "$CORE_PID" 2>/dev/null || true
  docker rm -f "$CH_NAME" >/dev/null 2>&1 || true
  rm -rf "$TMP_DIR"
}
trap cleanup EXIT

echo "[e2e] Building luma-eye binary..."
mkdir -p bin
CGO_ENABLED=0 go build -o bin/luma-eye ./cmd/luma-eye

echo "[e2e] Starting ClickHouse container ($CH_NAME) on port $CH_PORT..."
docker run -d --rm --name "$CH_NAME" \
  -p "127.0.0.1:${CH_PORT}:8123" \
  -e CLICKHOUSE_DB=luma_eye \
  -e CLICKHOUSE_DEFAULT_ACCESS_MANAGEMENT=1 \
  -v "${ROOT_DIR}/deploy/clickhouse/config.d/luma-eye.xml:/etc/clickhouse-server/config.d/luma-eye.xml:ro" \
  -v "${ROOT_DIR}/deploy/clickhouse/users.d/luma-eye.xml:/etc/clickhouse-server/users.d/luma-eye.xml:ro" \
  clickhouse/clickhouse-server:24.8-alpine >/dev/null

for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:${CH_PORT}/ping" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${CH_PORT}/ping" >/dev/null

echo "[e2e] Starting luma-eye Core on :$CORE_PORT..."
LUMA_EYE_MODE=core \
LUMA_EYE_LISTEN="127.0.0.1:${CORE_PORT}" \
LUMA_EYE_DATA_DIR="${TMP_DIR}/core-data" \
LUMA_EYE_ADMIN_TOKEN="$ADMIN_TOK" \
LUMA_EYE_RELAY_SECRET="$RELAY_TOK" \
LUMA_EYE_CLICKHOUSE_URL="http://127.0.0.1:${CH_PORT}" \
LUMA_EYE_CLICKHOUSE_DB=luma_eye \
LUMA_EYE_BATCH_INTERVAL=300ms \
  ./bin/luma-eye serve >"${TMP_DIR}/core.log" 2>&1 &
CORE_PID=$!

for _ in $(seq 1 45); do
  if curl -fsS "http://127.0.0.1:${CORE_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 1
done
curl -fsS "http://127.0.0.1:${CORE_PORT}/healthz" >/dev/null

export LUMA_EYE_URL="http://127.0.0.1:${CORE_PORT}"
export LUMA_EYE_ADMIN_TOKEN="$ADMIN_TOK"

echo "[e2e] Provisioning project demo-app..."
PROJ_JSON="$(./bin/luma-eye projects ensure demo-app --origins https://demo.example.com --json)"
PK="$(printf '%s' "$PROJ_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["project"]["public_key"])')"
DSN_ID="$(printf '%s' "$PROJ_JSON" | python3 -c 'import sys,json; print(json.load(sys.stdin)["project"]["dsn_id"])')"
echo "[e2e] Project demo-app provisioned (dsn_id=$DSN_ID, key=$PK)"

echo "[e2e] Uploading SourceMap v3..."
mkdir -p "${TMP_DIR}/maps"
cat > "${TMP_DIR}/maps/index-abc.js.map" <<'EOF'
{
  "version": 3,
  "file": "index-abc.js",
  "sources": ["src/App.tsx"],
  "sourcesContent": ["const a = 1;\nconst b = 2;\nfunction test() {\n  handleClick();\n}\nexport default test;"],
  "names": ["handleClick"],
  "mappings": "AAGAA"
}
EOF
./bin/luma-eye sourcemaps upload --project demo-app --release v1.0.0 --prefix assets/ "${TMP_DIR}/maps"

echo "[e2e] Starting luma-eye Edge on :$EDGE_PORT -> relaying Zstd batches to Core..."
LUMA_EYE_MODE=edge \
LUMA_EYE_LISTEN="127.0.0.1:${EDGE_PORT}" \
LUMA_EYE_DATA_DIR="${TMP_DIR}/edge-data" \
LUMA_EYE_CORE_URL="http://127.0.0.1:${CORE_PORT}" \
LUMA_EYE_RELAY_SECRET="$RELAY_TOK" \
LUMA_EYE_BATCH_INTERVAL=300ms \
  ./bin/luma-eye serve >"${TMP_DIR}/edge.log" 2>&1 &
EDGE_PID=$!

for _ in $(seq 1 30); do
  if curl -fsS "http://127.0.0.1:${EDGE_PORT}/healthz" >/dev/null 2>&1; then
    break
  fi
  sleep 0.5
done
curl -fsS "http://127.0.0.1:${EDGE_PORT}/healthz" >/dev/null

echo "[e2e] Sending Track 1 funnel + vitals + logs via Edge (text/plain zero-preflight)..."
NOW_SEC="$(python3 -c 'import time; print(int(time.time()))')"
TRACE_ID="0123456789abcdef0123456789abcdef"

curl -fsS -X POST "http://127.0.0.1:${EDGE_PORT}/_e/b" \
  -H "Content-Type: text/plain;charset=UTF-8" \
  -H "Origin: https://demo.example.com" \
  -d "{
    \"key\": \"${PK}\",
    \"project\": \"demo-app\",
    \"track\": \"normal\",
    \"env\": \"production\",
    \"release\": \"v1.0.0\",
    \"actor_id\": \"u_e2e_1\",
    \"session_id\": \"s_e2e_1\",
    \"page_url\": \"https://demo.example.com/\",
    \"events\": [
      {\"ts\": $((NOW_SEC - 10)), \"type\": \"pageview\", \"name\": \"pageview\", \"page_path\": \"/\", \"duration_ms\": 2100},
      {\"ts\": $((NOW_SEC - 5)), \"type\": \"custom\", \"name\": \"upload_code\", \"page_path\": \"/scan\"},
      {\"ts\": ${NOW_SEC}, \"type\": \"custom\", \"name\": \"scan_complete\", \"page_path\": \"/scan\", \"trace_id\": \"${TRACE_ID}\"}
    ],
    \"vitals\": [
      {\"ts\": ${NOW_SEC}, \"name\": \"LCP\", \"value\": 1420, \"rating\": \"good\", \"page_path\": \"/\"}
    ],
    \"logs\": [
      {\"ts\": ${NOW_SEC}, \"level\": \"error\", \"message\": \"scan worker timeout\", \"trace_id\": \"${TRACE_ID}\", \"attrs\": {\"retry\": 1}}
    ]
  }" >/dev/null

echo "[e2e] Sending Track 2 urgent crash with Ring Buffer breadcrumbs via Edge..."
curl -fsS -X POST "http://127.0.0.1:${EDGE_PORT}/_e/b" \
  -H "Content-Type: text/plain;charset=UTF-8" \
  -H "Origin: https://demo.example.com" \
  -d "{
    \"key\": \"${PK}\",
    \"project\": \"demo-app\",
    \"track\": \"urgent\",
    \"env\": \"production\",
    \"release\": \"v1.0.0\",
    \"actor_id\": \"u_e2e_1\",
    \"session_id\": \"s_e2e_1\",
    \"errors\": [{
      \"ts\": ${NOW_SEC},
      \"level\": \"error\",
      \"type\": \"RangeError\",
      \"message\": \"Invalid array length\",
      \"stack\": \"RangeError: Invalid array length\\n    at parseReport (https://demo.example.com/assets/other.js:12:4)\",
      \"trace_id\": \"${TRACE_ID}\",
      \"breadcrumbs\": [
        {\"ts\": $((NOW_SEC - 4)), \"category\": \"ui.click\", \"level\": \"info\", \"message\": \"button#upload\"},
        {\"ts\": $((NOW_SEC - 2)), \"category\": \"console\", \"level\": \"debug\", \"message\": \"parsing 120kb payload\"},
        {\"ts\": $((NOW_SEC - 1)), \"category\": \"http\", \"level\": \"info\", \"message\": \"POST /api/scan -> 200 (180ms)\"}
      ]
    }]
  }" >/dev/null

echo "[e2e] Sending gzipped Sentry Envelope crash for SourceMap symbolication via Edge..."
python3 - <<PY > "${TMP_DIR}/sentry.env.gz"
import gzip, json
ev = {
    "event_id": "11112222333344445555666677778888",
    "timestamp": ${NOW_SEC},
    "level": "error",
    "environment": "production",
    "release": "v1.0.0",
    "exception": {
        "values": [{
            "type": "TypeError",
            "value": "Cannot read properties of null",
            "stacktrace": {
                "frames": [{
                    "filename": "https://demo.example.com/assets/index-abc.js",
                    "function": "t",
                    "lineno": 1,
                    "colno": 1,
                    "in_app": True
                }]
            }
        }]
    }
}
ev_raw = json.dumps(ev)
hdr = json.dumps({"event_id": "11112222333344445555666677778888", "dsn": f"https://${PK}@127.0.0.1:${EDGE_PORT}/${DSN_ID}"})
item_hdr = json.dumps({"type": "event", "length": len(ev_raw)})
import sys
sys.stdout.buffer.write(gzip.compress((hdr + "\n" + item_hdr + "\n" + ev_raw + "\n").encode()))
PY

curl -fsS -X POST "http://127.0.0.1:${EDGE_PORT}/api/${DSN_ID}/envelope/?sentry_key=${PK}" \
  -H "Origin: https://demo.example.com" \
  --data-binary "@${TMP_DIR}/sentry.env.gz" >/dev/null

echo "[e2e] Waiting for Edge Zstd Relay -> Core -> ClickHouse flush..."
for _ in $(seq 1 30); do
  STATS_JSON="$(./bin/luma-eye stats demo-app --since 24h --json || true)"
  ERR_CNT="$(printf '%s' "$STATS_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("error_count",0))' 2>/dev/null || echo 0)"
  PV_CNT="$(printf '%s' "$STATS_JSON" | python3 -c 'import sys,json; d=json.load(sys.stdin); print(d.get("pageviews",0))' 2>/dev/null || echo 0)"
  if [[ "$ERR_CNT" -ge 2 && "$PV_CNT" -ge 1 ]]; then
    break
  fi
  sleep 0.5
done

echo "[e2e] Verifying stats, funnel, retention, symbolicated issues + breadcrumbs, logs, console, SDK, and Grafana JSON..."
STATS_JSON="$(./bin/luma-eye stats demo-app --since 24h --json)"
FUNNEL_JSON="$(./bin/luma-eye funnel demo-app --steps "pageview,upload_code,scan_complete" --window 3600 --since 7d --json)"
RET_JSON="$(./bin/luma-eye retention demo-app --days 7 --json)"
ISSUES_JSON="$(./bin/luma-eye issues demo-app --since 7d --status open --json)"
LOGS_JSON="$(./bin/luma-eye logs demo-app --trace "$TRACE_ID" --since 1h --json)"

python3 - "$STATS_JSON" "$FUNNEL_JSON" "$RET_JSON" "$ISSUES_JSON" "$LOGS_JSON" <<'PY'
import json, sys
stats = json.loads(sys.argv[1])
funnel = json.loads(sys.argv[2])
ret = json.loads(sys.argv[3])
issues = json.loads(sys.argv[4])["issues"]
logs = json.loads(sys.argv[5])["logs"]

assert stats["unique_visitors"] >= 1, stats
assert stats["pageviews"] >= 1, stats
assert stats["custom_events"] >= 2, stats
assert stats["error_count"] >= 2, stats
assert len(stats.get("timeseries", [])) >= 1, stats
assert any(v["metric_name"] == "LCP" and v["p75"] >= 1400 for v in stats.get("vitals_p75", [])), stats

assert abs(funnel["overall_conversion"] - 100.0) < 0.01, funnel
assert len(funnel["steps"]) == 3 and all(s["users"] >= 1 for s in funnel["steps"]), funnel

assert len(ret.get("rows", [])) >= 1 and ret["rows"][0]["day_counts"][0] >= 1, ret
assert len(issues) >= 2, issues
assert len(logs) >= 1 and logs[0]["message"] == "scan worker timeout", logs
print("ALL AGGREGATE ASSERTIONS PASSED")
PY

# Inspect every issue detail and assert both SourceMap symbolication AND Ring Buffer breadcrumbs arrived intact!
FOUND_SYM=0
FOUND_BC=0
for fp in $(printf '%s' "$ISSUES_JSON" | python3 -c 'import sys,json; [print(i["fingerprint"]) for i in json.load(sys.stdin)["issues"]]'); do
  DETAIL="$(./bin/luma-eye issues demo-app --detail "$fp" --json)"
  CHECK_RES="$(printf '%s' "$DETAIL" | python3 -c '
import sys, json
d = json.load(sys.stdin)
frames = d.get("frames") or []
bcs = d.get("breadcrumbs") or []
sym = any(f.get("symbolicated") and f.get("filename") == "src/App.tsx" and f.get("context_line") == "  handleClick();" for f in frames)
bc = len(bcs) == 3 and bcs[0]["category"] == "ui.click"
print(f"{int(sym)} {int(bc)}")
')"
  read -r IS_SYM IS_BC <<< "$CHECK_RES"
  [[ "$IS_SYM" == "1" ]] && FOUND_SYM=1
  [[ "$IS_BC" == "1" ]] && FOUND_BC=1
done

[[ "$FOUND_SYM" == "1" ]] || { echo "SourceMap symbolication assertion failed"; exit 1; }
[[ "$FOUND_BC" == "1" ]] || { echo "Ring Buffer breadcrumbs assertion failed"; exit 1; }

curl -fsS "http://127.0.0.1:${CORE_PORT}/console/" | grep -q "Luma Eye"
curl -fsS "http://127.0.0.1:${EDGE_PORT}/_e/eye.js" | grep -q "LumaEye"
./bin/luma-eye grafana render --project demo-app | grep -q '"luma-app:demo-app"'

echo "[e2e] SUCCESS: Full Split Edge -> Zstd Relay -> Core -> ClickHouse + SourceMap + Ring Buffer E2E passed!"
