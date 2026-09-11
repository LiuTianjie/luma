# Application instrumentation contract

This is the application-facing contract for `luma-observe`. It uses official
OpenTelemetry APIs and exporters; there is no Luma telemetry SDK.

## What deployment provides

After `luma-observe` is active, Control injects these settings into **new
deployments**:

- `OTEL_SERVICE_NAME`
- `OTEL_RESOURCE_ATTRIBUTES` with `luma.stack`, `luma.task`, and `luma.region`
- `OTEL_EXPORTER_OTLP_ENDPOINT` using the manager mesh listener
- `OTEL_EXPORTER_OTLP_PROTOCOL=http/protobuf`
- `OTEL_EXPORTER_OTLP_HEADERS` with the Control-issued bearer token

Existing allocations need a new deployment to receive them. A restart alone is
not enough. Application code must not copy the endpoint or token into source,
logs, images, or its own configuration. The official SDK reads the environment.

## Minimum application setup

1. Add the official language distro and OTLP exporter to the image.
2. Start the process with the language's supported auto-instrumentation entrypoint
   when automatic HTTP/database/client spans are wanted.
3. Use the official tracing API for business operations that automatic
   instrumentation cannot name or see.
4. Keep exporter failure open: an unavailable collector must not fail the request.

Python packages commonly include `opentelemetry-distro` and
`opentelemetry-exporter-otlp`; start with `opentelemetry-instrument` when the
image and framework support it. Node, Java, and other runtimes must use their
official OpenTelemetry distro/agent and documented startup mechanism.

## Business span naming

Use a stable, low-cardinality action name in dot notation:

```text
document.convert
payment.create
homework.grade
queue.consume
```

Do not put IDs, URLs, filenames, query text, user text, tokens, phone numbers,
or request bodies in span names. Use attributes only for safe dimensions that
are useful during investigation:

```text
document.format = "docx"
queue.name = "video"
outcome = "success" | "error"
```

Keep IDs out of metric labels. If an operation ID is needed to jump from an
incident to logs, record a redacted or short-lived correlation value as a span
attribute and in structured logs, subject to the application's privacy rules.

## Manual span rules

- Start a span at a meaningful business boundary, not around every helper.
- Set status to error and record the exception before ending failed spans.
- End spans in a `finally`/defer path so failures are visible.
- Preserve parent context across HTTP, queue, and async boundaries using the
  official propagation library.
- Never make a user request wait for a trace export or expose exporter errors.

Python example:

```python
from opentelemetry import trace

tracer = trace.get_tracer("word2pdf")

def convert_document(document_id: str):
    with tracer.start_as_current_span("document.convert") as span:
        span.set_attribute("document.format", "docx")
        try:
            return do_convert(document_id)
        except Exception as exc:
            span.record_exception(exc)
            span.set_status(trace.StatusCode.ERROR)
            raise
```

Node example:

```javascript
const { trace, SpanStatusCode } = require("@opentelemetry/api");
const tracer = trace.getTracer("word2pdf");

async function convertDocument() {
  return tracer.startActiveSpan("document.convert", async (span) => {
    try {
      return await doConvert();
    } catch (error) {
      span.recordException(error);
      span.setStatus({ code: SpanStatusCode.ERROR });
      throw error;
    } finally {
      span.end();
    }
  });
}
```

## Metrics and sampling expectations

The collector keeps HTTP 5xx, OpenTelemetry errors, traces slower than one
second, and a 10% baseline sample. Do not assume every successful fast request
has a stored trace. Span names and safe attributes may later feed spanmetrics
for request count, error rate, and latency alerts; high-cardinality attributes
must never become metric labels.

Do not add an application collector sidecar by default. The shared collector is
the supported path. eBPF is an optional Linux-only infrastructure supplement
for network/process visibility; it does not replace business spans.
