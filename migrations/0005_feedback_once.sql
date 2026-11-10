-- A feedback link acts once: the same signal on the same digest, event, source and item
-- is ignored the second time. Earlier repeats are removed first.
DELETE FROM feedback WHERE id NOT IN (
  SELECT MIN(id) FROM feedback
  GROUP BY digest_id, event_id, source_id, item_id, signal
);

CREATE UNIQUE INDEX feedback_once ON feedback (
  COALESCE(digest_id, ''),
  COALESCE(event_id, ''),
  COALESCE(source_id, ''),
  COALESCE(item_id, ''),
  signal
);
