#!/bin/sh
set -eu

: "${LAE_MINIO_ROOT_USER:?LAE_MINIO_ROOT_USER is required}"
: "${LAE_MINIO_ROOT_PASSWORD:?LAE_MINIO_ROOT_PASSWORD is required}"
: "${LAE_S3_API_ACCESS_KEY:?LAE_S3_API_ACCESS_KEY is required}"
: "${LAE_S3_API_SECRET_KEY:?LAE_S3_API_SECRET_KEY is required}"
: "${LAE_S3_WORKER_ACCESS_KEY:?LAE_S3_WORKER_ACCESS_KEY is required}"
: "${LAE_S3_WORKER_SECRET_KEY:?LAE_S3_WORKER_SECRET_KEY is required}"

endpoint=${LAE_MINIO_INTERNAL_ENDPOINT:-http://artifact-store:9000}
upload_bucket=${LAE_UPLOAD_S3_BUCKET:-lae-uploads}
artifact_bucket=${LAE_ARTIFACT_S3_BUCKET:-lae-artifacts}
alias_name=lae-root
mc_bin=$(command -v mc)

attempt=0
until "$mc_bin" alias set "$alias_name" "$endpoint" "$LAE_MINIO_ROOT_USER" "$LAE_MINIO_ROOT_PASSWORD" >/dev/null 2>&1; do
  attempt=$((attempt + 1))
  if [ "$attempt" -ge 60 ]; then
    echo "artifact store did not become ready" >&2
    exit 1
  fi
  sleep 2
done

"$mc_bin" mb --ignore-existing "$alias_name/$upload_bucket" >/dev/null
"$mc_bin" mb --ignore-existing "$alias_name/$artifact_bucket" >/dev/null
"$mc_bin" anonymous set none "$alias_name/$upload_bucket" >/dev/null
"$mc_bin" anonymous set none "$alias_name/$artifact_bucket" >/dev/null
"$mc_bin" version enable "$alias_name/$artifact_bucket" >/dev/null

umask 077
cat >/tmp/lae-api-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": ["arn:aws:s3:::$upload_bucket/tenants/*/apps/*/quarantine/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion"],
      "Resource": ["arn:aws:s3:::$artifact_bucket/tenants/*/analysis-artifacts/*"]
    }
  ]
}
EOF

cat >/tmp/lae-worker-policy.json <<EOF
{
  "Version": "2012-10-17",
  "Statement": [
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:DeleteObject"],
      "Resource": ["arn:aws:s3:::$upload_bucket/tenants/*/apps/*/quarantine/*"]
    },
    {
      "Effect": "Allow",
      "Action": ["s3:GetObject", "s3:GetObjectVersion", "s3:PutObject", "s3:DeleteObject", "s3:AbortMultipartUpload"],
      "Resource": ["arn:aws:s3:::$artifact_bucket/tenants/*/analysis-artifacts/*"]
    }
  ]
}
EOF

# Re-running this initializer rotates an existing user's secret and overwrites
# the named policy, so secret rotation and policy tightening are convergent.
"$mc_bin" admin user add "$alias_name" "$LAE_S3_API_ACCESS_KEY" "$LAE_S3_API_SECRET_KEY" >/dev/null
"$mc_bin" admin user add "$alias_name" "$LAE_S3_WORKER_ACCESS_KEY" "$LAE_S3_WORKER_SECRET_KEY" >/dev/null
"$mc_bin" admin policy create "$alias_name" lae-api /tmp/lae-api-policy.json >/dev/null
"$mc_bin" admin policy create "$alias_name" lae-worker /tmp/lae-worker-policy.json >/dev/null
"$mc_bin" admin policy attach "$alias_name" lae-api --user "$LAE_S3_API_ACCESS_KEY" >/dev/null
"$mc_bin" admin policy attach "$alias_name" lae-worker --user "$LAE_S3_WORKER_ACCESS_KEY" >/dev/null
rm -f /tmp/lae-api-policy.json /tmp/lae-worker-policy.json
touch /tmp/lae-artifact-init.ready

# Luma currently renders every Compose service as a long-running Nomad task.
# Keep this idempotent initializer healthy after applying the desired state.
while :; do
  sleep 86400
done
