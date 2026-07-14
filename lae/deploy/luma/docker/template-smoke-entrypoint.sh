#!/bin/sh
set -eu

interval="${LAE_TEMPLATE_SMOKE_INTERVAL_SECONDS:-86400}"
start_delay="${LAE_TEMPLATE_SMOKE_START_DELAY_SECONDS:-300}"

case "$interval:$start_delay" in
  *[!0-9:]*|:*|*:)
    echo "template-smoke: invalid schedule" >&2
    exit 2
    ;;
esac
if [ "$interval" -lt 3600 ] || [ "$start_delay" -gt 3600 ]; then
  echo "template-smoke: schedule is outside the admitted range" >&2
  exit 2
fi

touch /tmp/lae-template-smoke-heartbeat
sleep "$start_delay"
while :; do
  started="$(date -u +%Y-%m-%dT%H:%M:%SZ)"
  if python /opt/lae/scripts/template_smoke.py; then
    status=0
  else
    status=$?
  fi
  touch /tmp/lae-template-smoke-heartbeat
  printf '{"event":"template_smoke_schedule","startedAt":"%s","status":%s}\n' "$started" "$status"
  sleep "$interval"
done
