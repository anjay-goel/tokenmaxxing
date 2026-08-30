CREATE INDEX idx_issues_active_identity
ON issues(source, category, identifier, field_path)
WHERE resolved_at_ns IS NULL;
