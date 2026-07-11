#!/bin/sh
set -u

child_pid=""

stop() {
  if [ -n "$child_pid" ]; then
    kill -TERM "$child_pid" 2>/dev/null || true
    wait "$child_pid" 2>/dev/null || true
  fi
  exit 0
}

trap stop INT TERM

while :; do
  lae-worker --once &
  child_pid=$!
  wait "$child_pid"
  status=$?
  child_pid=""

  case "$status" in
    0)
      delay="${LAE_WORKER_IDLE_SECONDS:-2}"
      ;;
    2)
      delay="${LAE_WORKER_RETRY_SECONDS:-10}"
      ;;
    *)
      exit "$status"
      ;;
  esac

  sleep "$delay" &
  child_pid=$!
  wait "$child_pid" || true
  child_pid=""
done
