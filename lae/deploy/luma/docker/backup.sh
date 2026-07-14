#!/bin/sh
set -eu

mode=${1:-run}
backup_root=${LAE_BACKUP_ROOT:-/backups}
snapshot_root="$backup_root/snapshots"
last_success="$backup_root/last-success.epoch"
last_drill="$backup_root/last-restore-drill.epoch"
interval=${LAE_BACKUP_INTERVAL_SECONDS:-86400}
retention_days=${LAE_BACKUP_RETENTION_DAYS:-7}
max_age=${LAE_BACKUP_MAX_AGE_SECONDS:-93600}

require_configuration() {
  : "${PGHOST:?PGHOST is required}"
  : "${PGUSER:?PGUSER is required}"
  : "${PGDATABASE:?PGDATABASE is required}"
  : "${PGPASSWORD:?PGPASSWORD is required}"
  : "${LAE_MINIO_INTERNAL_ENDPOINT:?LAE_MINIO_INTERNAL_ENDPOINT is required}"
  : "${LAE_MINIO_ROOT_USER:?LAE_MINIO_ROOT_USER is required}"
  : "${LAE_MINIO_ROOT_PASSWORD:?LAE_MINIO_ROOT_PASSWORD is required}"
  : "${LAE_UPLOAD_S3_BUCKET:?LAE_UPLOAD_S3_BUCKET is required}"
  : "${LAE_ARTIFACT_S3_BUCKET:?LAE_ARTIFACT_S3_BUCKET is required}"
}

configure_minio() {
  mc alias set lae-source "$LAE_MINIO_INTERNAL_ENDPOINT" \
    "$LAE_MINIO_ROOT_USER" "$LAE_MINIO_ROOT_PASSWORD" >/dev/null
}

snapshot_bucket() {
  bucket=$1
  destination=$2
  mkdir -p "$destination"
  mc mirror --overwrite --preserve "lae-source/$bucket" "$destination" >/dev/null
}

write_checksums() {
  directory=$1
  (
    cd "$directory"
    find postgres.dump postgres.list objects -type f -print0 \
      | sort -z \
      | xargs -0 sha256sum >SHA256SUMS
  )
}

restore_bucket_drill() {
  snapshot=$1
  source_bucket=$2
  suffix=$3
  restore_bucket="lae-restore-drill-$suffix"
  mc mb --ignore-existing "lae-source/$restore_bucket" >/dev/null
  cleanup_bucket() {
    mc rb --force "lae-source/$restore_bucket" >/dev/null 2>&1 || true
  }
  if ! mc mirror --overwrite --preserve "$snapshot/objects/$source_bucket" \
    "lae-source/$restore_bucket" >/dev/null; then
    cleanup_bucket
    return 1
  fi
  local_count=$(find "$snapshot/objects/$source_bucket" -type f | wc -l | tr -d ' ')
  remote_count=$(mc find "lae-source/$restore_bucket" --type f | wc -l | tr -d ' ')
  if [ "$local_count" != "$remote_count" ]; then
    echo "restored object count mismatch" >&2
    exit 1
  fi
  cleanup_bucket
}

run_restore_drill() {
  snapshot=$1
  suffix=$(date -u +%Y%m%d%H%M%S)-$$
  database="lae_restore_$suffix"
  database=$(printf '%s' "$database" | tr -cd 'a-zA-Z0-9_')
  cleanup_database() {
    dropdb --if-exists "$database" >/dev/null 2>&1 || true
  }
  trap cleanup_database EXIT HUP INT TERM

  (cd "$snapshot" && sha256sum -c SHA256SUMS >/dev/null)
  createdb "$database"
  pg_restore --exit-on-error --no-owner --no-privileges \
    --dbname "$database" "$snapshot/postgres.dump" >/dev/null
  psql --dbname "$database" --no-psqlrc --tuples-only --no-align \
    --command "SELECT count(*) FROM information_schema.tables WHERE table_schema = 'public';" \
    | grep -Eq '^[1-9][0-9]*$'

  restore_bucket_drill "$snapshot" "$LAE_UPLOAD_S3_BUCKET" "uploads-$suffix"
  restore_bucket_drill "$snapshot" "$LAE_ARTIFACT_S3_BUCKET" "artifacts-$suffix"
  cleanup_database
  trap - EXIT HUP INT TERM
  date +%s >"$last_drill"
}

run_backup() {
  require_configuration
  configure_minio
  mkdir -p "$snapshot_root"
  chmod 0700 "$backup_root" "$snapshot_root"
  timestamp=$(date -u +%Y%m%dT%H%M%SZ)
  work="$snapshot_root/.${timestamp}.$$"
  final="$snapshot_root/$timestamp"
  mkdir -m 0700 "$work"
  cleanup_work() {
    rm -rf "$work"
  }
  trap cleanup_work EXIT HUP INT TERM

  pg_dump --format=custom --compress=6 --file "$work/postgres.dump"
  pg_restore --list "$work/postgres.dump" >"$work/postgres.list"
  snapshot_bucket "$LAE_UPLOAD_S3_BUCKET" "$work/objects/$LAE_UPLOAD_S3_BUCKET"
  snapshot_bucket "$LAE_ARTIFACT_S3_BUCKET" "$work/objects/$LAE_ARTIFACT_S3_BUCKET"
  write_checksums "$work"
  database_bytes=$(wc -c <"$work/postgres.dump" | tr -d ' ')
  object_files=$(find "$work/objects" -type f | wc -l | tr -d ' ')
  printf '{"schemaVersion":"lae.backup/v1","createdAt":"%s","databaseBytes":%s,"objectFiles":%s}\n' \
    "$timestamp" "$database_bytes" "$object_files" >"$work/manifest.json"
  chmod -R go-rwx "$work"
  mv "$work" "$final"
  trap - EXIT HUP INT TERM
  ln -sfn "$timestamp" "$snapshot_root/latest"
  date +%s >"$last_success"

  if [ "${LAE_BACKUP_RUN_RESTORE_DRILL:-0}" = "1" ]; then
    run_restore_drill "$final"
  fi
  find "$snapshot_root" -mindepth 1 -maxdepth 1 -type d \
    -mtime "+$retention_days" -exec rm -rf {} +
  echo "LAE backup and isolated restore drill completed: $timestamp"
}

health() {
  if [ ! -r "$last_success" ] || [ ! -r "$last_drill" ]; then
    exit 1
  fi
  now=$(date +%s)
  backup_age=$((now - $(cat "$last_success")))
  drill_age=$((now - $(cat "$last_drill")))
  [ "$backup_age" -ge 0 ] && [ "$backup_age" -le "$max_age" ]
  [ "$drill_age" -ge 0 ] && [ "$drill_age" -le "$max_age" ]
}

case "$mode" in
  once)
    run_backup
    ;;
  health)
    health
    ;;
  run)
    while :; do
      run_backup
      sleep "$interval"
    done
    ;;
  *)
    echo "usage: lae-backup {run|once|health}" >&2
    exit 2
    ;;
esac
