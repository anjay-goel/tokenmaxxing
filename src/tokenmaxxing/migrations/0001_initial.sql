CREATE TABLE sessions (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    source_session_id TEXT NOT NULL,
    root_session_id TEXT,
    parent_session_id TEXT,
    harness_version TEXT,
    schema_version TEXT,
    provider TEXT,
    initial_model TEXT,
    current_model TEXT,
    reasoning_effort TEXT,
    service_tier TEXT,
    started_at_ns INTEGER,
    updated_at_ns INTEGER,
    completed_at_ns INTEGER,
    archived_at_ns INTEGER,
    workspace_hash TEXT,
    is_complete INTEGER,
    is_archived INTEGER,
    UNIQUE (source, source_session_id)
);

CREATE TABLE runs (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    source_run_id TEXT NOT NULL,
    parent_run_id TEXT,
    batch_id TEXT,
    workflow_id TEXT,
    agent_id TEXT,
    role TEXT,
    status TEXT,
    model TEXT,
    provider TEXT,
    effort TEXT,
    isolation TEXT,
    started_at_ns INTEGER,
    completed_at_ns INTEGER,
    duration_ns INTEGER,
    depth INTEGER,
    UNIQUE (session_id, source_run_id)
);

CREATE TABLE turns (
    id INTEGER PRIMARY KEY,
    session_id INTEGER NOT NULL REFERENCES sessions(id),
    run_id INTEGER REFERENCES runs(id),
    source_turn_id TEXT NOT NULL,
    source_run_id TEXT,
    started_at_ns INTEGER,
    completed_at_ns INTEGER,
    duration_ns INTEGER,
    ttft_ns INTEGER,
    model TEXT,
    effort TEXT,
    service_tier TEXT,
    status TEXT,
    error_category TEXT,
    UNIQUE (session_id, source_turn_id)
);

CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    event_key TEXT NOT NULL,
    session_id INTEGER REFERENCES sessions(id),
    run_id INTEGER REFERENCES runs(id),
    turn_id INTEGER REFERENCES turns(id),
    response_id TEXT,
    request_id TEXT,
    client_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    source_sequence INTEGER,
    granularity TEXT NOT NULL,
    status TEXT NOT NULL,
    provider TEXT,
    api TEXT,
    model TEXT,
    response_model TEXT,
    service_tier TEXT,
    speed TEXT,
    inference_region TEXT,
    effort TEXT,
    stop_reason TEXT,
    error_category TEXT,
    started_at_ns INTEGER,
    completed_at_ns INTEGER,
    duration_ns INTEGER,
    ttft_ns INTEGER,
    retries INTEGER,
    success INTEGER,
    status_code INTEGER,
    web_search_count INTEGER,
    web_fetch_count INTEGER,
    tool_use_count INTEGER,
    input_tokens INTEGER,
    output_tokens INTEGER,
    cache_read_tokens INTEGER,
    cache_write_tokens INTEGER,
    cache_write_5m_tokens INTEGER,
    cache_write_1h_tokens INTEGER,
    reasoning_tokens INTEGER,
    reported_total_tokens INTEGER,
    derived_total_tokens INTEGER,
    input_cost_nanos INTEGER,
    output_cost_nanos INTEGER,
    cache_read_cost_nanos INTEGER,
    cache_write_cost_nanos INTEGER,
    total_cost_nanos INTEGER,
    original_cost_decimal TEXT,
    cost_source TEXT,
    cost_estimated INTEGER,
    UNIQUE (source, event_key)
);

CREATE TABLE observations (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    event_type TEXT NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    parser_version TEXT NOT NULL,
    source_session_id TEXT,
    source_run_id TEXT,
    source_turn_id TEXT,
    response_id TEXT,
    request_id TEXT,
    client_id TEXT,
    trace_id TEXT,
    span_id TEXT,
    source_sequence INTEGER,
    artifact_id INTEGER REFERENCES artifacts(id),
    ordinal INTEGER,
    projection_json TEXT NOT NULL,
    UNIQUE (source, channel, stable_key)
);

CREATE TABLE observation_links (
    observation_id INTEGER NOT NULL REFERENCES observations(id),
    usage_event_id INTEGER NOT NULL REFERENCES usage_events(id),
    method TEXT NOT NULL,
    role TEXT NOT NULL,
    confidence TEXT NOT NULL,
    PRIMARY KEY (observation_id, usage_event_id)
);

CREATE TABLE samples (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    channel TEXT NOT NULL,
    stable_key TEXT NOT NULL,
    sample_type TEXT NOT NULL,
    observed_at_ns INTEGER NOT NULL,
    name TEXT NOT NULL,
    unit TEXT,
    value_integer INTEGER,
    value_real REAL,
    attributes_json TEXT NOT NULL
);

CREATE TABLE artifacts (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    path_hash TEXT NOT NULL,
    device INTEGER,
    inode INTEGER,
    generation INTEGER NOT NULL,
    size_bytes INTEGER,
    mtime_ns INTEGER,
    byte_offset INTEGER,
    prefix_fingerprint TEXT,
    header_session_id TEXT,
    parser_version TEXT,
    last_seen_at_ns INTEGER,
    is_missing INTEGER,
    UNIQUE (source, path_hash, generation)
);

CREATE TABLE ingest_runs (
    id INTEGER PRIMARY KEY,
    source TEXT,
    channel TEXT,
    started_at_ns INTEGER NOT NULL,
    completed_at_ns INTEGER,
    status TEXT,
    artifact_count INTEGER,
    observation_count INTEGER,
    event_count INTEGER,
    issue_count INTEGER
);

CREATE TABLE issues (
    id INTEGER PRIMARY KEY,
    source TEXT NOT NULL,
    category TEXT NOT NULL,
    severity TEXT NOT NULL,
    identifier TEXT NOT NULL,
    field_path TEXT,
    observed_type TEXT,
    first_seen_at_ns INTEGER,
    last_seen_at_ns INTEGER,
    resolved_at_ns INTEGER
);

CREATE INDEX idx_sessions_source_updated_at ON sessions(source, updated_at_ns);
CREATE INDEX idx_usage_events_source_model ON usage_events(source, model);
CREATE INDEX idx_usage_events_status ON usage_events(status);
CREATE INDEX idx_usage_events_response_id ON usage_events(response_id);
CREATE INDEX idx_usage_events_request_id ON usage_events(request_id);
CREATE INDEX idx_observations_response_id ON observations(response_id);
CREATE INDEX idx_observations_request_id ON observations(request_id);
CREATE INDEX idx_observation_links_usage_event ON observation_links(usage_event_id);
CREATE INDEX idx_artifacts_lookup ON artifacts(source, path_hash, last_seen_at_ns);

CREATE VIEW counted_usage_events AS
SELECT *
FROM usage_events
WHERE status IN ('canonical', 'provisional');
