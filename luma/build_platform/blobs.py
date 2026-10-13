from __future__ import annotations

import gzip
import hashlib
import json

from .config import Settings


class Blobs:
    def __init__(self, settings: Settings):
        import boto3
        from botocore.config import Config
        self.bucket = settings.s3_bucket
        self.client = boto3.client("s3", endpoint_url=settings.s3_endpoint or None, region_name=settings.s3_region,
                                   config=Config(signature_version="s3v4", retries={"max_attempts": 3}, connect_timeout=5, read_timeout=30))

    def put_file(self, key: str, path):
        self.client.upload_file(str(path), self.bucket, key)

    def url(self, key: str, *, expires=900) -> str:
        return self.client.generate_presigned_url("get_object", Params={"Bucket": self.bucket, "Key": key}, ExpiresIn=expires)

    def append(self, store, run_id: str, batch_id: str, events: list[dict]) -> dict:
        raw = json.dumps(events, separators=(",", ":"), ensure_ascii=False).encode()
        checksum = hashlib.sha256(raw).hexdigest()
        key = f"logs/{run_id}/{checksum}.json.gz"
        self.client.put_object(Bucket=self.bucket, Key=key, Body=gzip.compress(raw), ContentType="application/json", ContentEncoding="gzip")
        return store.append_chunk(run_id, batch_id, key, checksum, len(events))

    def events(self, store, run_id: str, after: int, until: int, limit: int) -> list[dict]:
        events = []
        for chunk in store.chunks(run_id, after, until, limit):
            response = self.client.get_object(Bucket=self.bucket, Key=chunk["object_key"])
            try:
                raw = gzip.decompress(response["Body"].read())
            finally:
                response["Body"].close()
            if hashlib.sha256(raw).hexdigest() != chunk["checksum"]:
                raise ValueError("archived log chunk checksum mismatch")
            for index, event in enumerate(json.loads(raw)):
                position = chunk["first_seq"] + index
                if after < position <= until:
                    events.append(event)
                    if len(events) == limit:
                        return events
        return events

    def milestone(self, store, run_id: str, key: str, name: str, message: str, status="ok"):
        return self.append(store, run_id, "stage-" + key, [{"name": name, "status": status, "message": message, "ts": store.get(run_id)["created_at"]}])
