# `luma-eye` (Python SDK)

Zero-dependency Dual-Track telemetry & Ring Buffer flight-recorder SDK for Python backend services, scripts, and AI Agents reporting to Luma Eye.

```python
from luma_eye import init_luma_eye, track, add_breadcrumb, capture_exception

eye = init_luma_eye(
    endpoint="https://eye.example.com",
    key="pk_your_project_key",
    project="my-service",
    env="production",
)

add_breadcrumb("pipeline", "parsed 120 rows")
track("job_completed", {"rows": 120})
```
