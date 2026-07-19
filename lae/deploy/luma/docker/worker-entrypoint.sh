#!/bin/sh
set -u

children=""

stop() {
  trap - INT TERM
  for pid in $children; do
    kill -TERM "$pid" 2>/dev/null || true
  done
  for pid in $children; do
    wait "$pid" 2>/dev/null || true
  done
  exit 0
}

trap stop INT TERM

run_slot() {
  slot="$1"
  base_worker_id="${LAE_WORKER_ID:-lae-worker}"
  export LAE_WORKER_ID="${base_worker_id}-slot-${slot}"

  while :; do
    lae-worker --once
    status=$?

    case "$status" in
      0)
        delay="${LAE_WORKER_IDLE_SECONDS:-2}"
        ;;
      2)
        delay="${LAE_WORKER_RETRY_SECONDS:-10}"
        ;;
      *)
        return "$status"
        ;;
    esac

    sleep "$delay"
  done
}

concurrency="${LAE_WORKER_CONCURRENCY:-3}"
case "$concurrency" in
  ''|*[!0-9]*|0) echo "LAE_WORKER_CONCURRENCY must be a positive integer" >&2; exit 2 ;;
esac

slot=1
while [ "$slot" -le "$concurrency" ]; do
  run_slot "$slot" &
  children="$children $!"
  slot=$((slot + 1))
done

for pid in $children; do
  wait "$pid" || exit $?
done
