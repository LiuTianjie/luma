CREATE DATABASE IF NOT EXISTS {{DB}};

CREATE TABLE IF NOT EXISTS {{DB}}.events (
    ts DateTime64(3, 'UTC'),
    project LowCardinality(String),
    env LowCardinality(String),
    release String,
    event_type LowCardinality(String),
    event_name LowCardinality(String),
    actor_id String,
    session_id String,
    page_path String,
    page_url String,
    referrer String,
    utm_source LowCardinality(String),
    utm_medium LowCardinality(String),
    utm_campaign String,
    duration_ms UInt32,
    browser LowCardinality(String),
    os LowCardinality(String),
    device_type LowCardinality(String),
    country LowCardinality(String),
    trace_id String,
    props_json String,
    client_ip_hash String
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (project, event_type, event_name, toStartOfHour(ts), ts)
TTL toDateTime(ts) + INTERVAL {{RETENTION_DAYS}} DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS {{DB}}.events_hourly (
    hour DateTime('UTC'),
    project LowCardinality(String),
    env LowCardinality(String),
    event_type LowCardinality(String),
    event_name LowCardinality(String),
    event_count SimpleAggregateFunction(sum, UInt64),
    uv_state AggregateFunction(uniqCombined(12), String),
    session_state AggregateFunction(uniqCombined(12), String)
) ENGINE = AggregatingMergeTree()
PARTITION BY toYYYYMMDD(hour)
ORDER BY (project, env, hour, event_type, event_name)
TTL hour + INTERVAL {{RETENTION_DAYS}} DAY DELETE
SETTINGS ttl_only_drop_parts = 1;

CREATE MATERIALIZED VIEW IF NOT EXISTS {{DB}}.events_hourly_mv
TO {{DB}}.events_hourly AS
SELECT
    toStartOfHour(ts) AS hour,
    project,
    env,
    event_type,
    event_name,
    toUInt64(count()) AS event_count,
    uniqCombinedState(12)(actor_id) AS uv_state,
    uniqCombinedState(12)(session_id) AS session_state
FROM {{DB}}.events
GROUP BY hour, project, env, event_type, event_name;

CREATE TABLE IF NOT EXISTS {{DB}}.exceptions (
    ts DateTime64(3, 'UTC'),
    project LowCardinality(String),
    env LowCardinality(String),
    release String,
    fingerprint String,
    level LowCardinality(String),
    error_type LowCardinality(String),
    error_message String,
    culprit String,
    actor_id String,
    session_id String,
    page_url String,
    trace_id String,
    occurrence_weight UInt32 DEFAULT 1,
    frames_json String,
    breadcrumbs_json String,
    tags_json String,
    extra_json String,
    browser LowCardinality(String),
    os LowCardinality(String),
    device_type LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (project, fingerprint, ts)
TTL toDateTime(ts) + INTERVAL {{RETENTION_DAYS}} DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS {{DB}}.issue_rollups (
    project LowCardinality(String),
    fingerprint String,
    first_seen SimpleAggregateFunction(min, DateTime64(3, 'UTC')),
    last_seen SimpleAggregateFunction(max, DateTime64(3, 'UTC')),
    total_count SimpleAggregateFunction(sum, UInt64),
    users_state AggregateFunction(uniqCombined(12), String),
    error_type SimpleAggregateFunction(anyLast, String),
    error_message SimpleAggregateFunction(anyLast, String),
    culprit SimpleAggregateFunction(anyLast, String),
    level SimpleAggregateFunction(anyLast, String),
    last_release SimpleAggregateFunction(anyLast, String)
) ENGINE = AggregatingMergeTree()
ORDER BY (project, fingerprint)
TTL toDateTime(last_seen) + INTERVAL {{RETENTION_DAYS}} DAY DELETE;

CREATE MATERIALIZED VIEW IF NOT EXISTS {{DB}}.issue_rollups_mv
TO {{DB}}.issue_rollups AS
SELECT
    project,
    fingerprint,
    min(ts) AS first_seen,
    max(ts) AS last_seen,
    sum(toUInt64(occurrence_weight)) AS total_count,
    uniqCombinedState(12)(actor_id) AS users_state,
    anyLast(error_type) AS error_type,
    anyLast(error_message) AS error_message,
    anyLast(culprit) AS culprit,
    anyLast(level) AS level,
    anyLast(release) AS last_release
FROM {{DB}}.exceptions
GROUP BY project, fingerprint;

CREATE TABLE IF NOT EXISTS {{DB}}.logs (
    ts DateTime64(3, 'UTC'),
    project LowCardinality(String),
    env LowCardinality(String),
    release String,
    level LowCardinality(String),
    message String,
    actor_id String,
    session_id String,
    page_url String,
    trace_id String,
    attrs_json String,
    INDEX idx_msg message TYPE tokenbf_v1(16384, 3, 0) GRANULARITY 4
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (project, level, ts)
TTL toDateTime(ts) + INTERVAL {{RETENTION_DAYS}} DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS {{DB}}.vitals (
    ts DateTime64(3, 'UTC'),
    project LowCardinality(String),
    env LowCardinality(String),
    release String,
    metric_name LowCardinality(String),
    value Float64,
    rating LowCardinality(String),
    page_path String,
    actor_id String,
    session_id String,
    browser LowCardinality(String),
    device_type LowCardinality(String)
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (project, metric_name, ts)
TTL toDateTime(ts) + INTERVAL {{RETENTION_DAYS}} DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;

CREATE TABLE IF NOT EXISTS {{DB}}.replay_segments (
    ts DateTime64(3, 'UTC'),
    project LowCardinality(String),
    env LowCardinality(String),
    release String,
    session_id String,
    actor_id String,
    seq UInt32,
    start_ms Int64,
    end_ms Int64,
    events_count UInt32,
    has_full_snapshot UInt8,
    page_url String,
    viewport_w UInt16,
    viewport_h UInt16,
    events_json String CODEC(ZSTD(3))
) ENGINE = MergeTree()
PARTITION BY toYYYYMMDD(ts)
ORDER BY (project, session_id, start_ms, seq)
TTL toDateTime(ts) + INTERVAL {{RETENTION_DAYS}} DAY DELETE
SETTINGS index_granularity = 8192, ttl_only_drop_parts = 1;
