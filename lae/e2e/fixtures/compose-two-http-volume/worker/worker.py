from __future__ import annotations

import time
from datetime import UTC, datetime
from pathlib import Path


heartbeat = Path("/data/worker-heartbeat.txt")
while True:
    heartbeat.parent.mkdir(parents=True, exist_ok=True)
    heartbeat.write_text(datetime.now(UTC).isoformat(), encoding="utf-8")
    time.sleep(10)

