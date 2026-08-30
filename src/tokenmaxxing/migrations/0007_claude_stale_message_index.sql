CREATE INDEX idx_observations_claude_turn_artifact
ON observations(source, channel, source_turn_id, artifact_id);
