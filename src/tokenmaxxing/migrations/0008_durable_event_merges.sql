ALTER TABLE usage_events ADD COLUMN merged_into_id INTEGER REFERENCES usage_events(id);

CREATE TRIGGER preserve_merged_event_exclusion
AFTER UPDATE OF status, merged_into_id ON usage_events
WHEN NEW.merged_into_id IS NOT NULL AND NEW.status != 'excluded'
BEGIN
    UPDATE usage_events SET status = 'excluded' WHERE id = NEW.id;
END;
