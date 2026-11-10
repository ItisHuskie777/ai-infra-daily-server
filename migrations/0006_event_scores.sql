-- The model's last score for each event, so an unchanged event is not scored again.
ALTER TABLE events ADD COLUMN score_json TEXT;
