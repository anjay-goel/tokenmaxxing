CREATE INDEX idx_observations_codex_owner_artifact
ON observations(source, channel, event_type, source_session_id, artifact_id);

CREATE INDEX idx_artifacts_source_path_generation
ON artifacts(source, path_hash, generation);
