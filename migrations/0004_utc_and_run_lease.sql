-- Timestamps were stored with each source's own UTC offset and compared as text,
-- which misorders them by up to fourteen hours. Every stored timestamp becomes UTC
-- with a fixed width (the format of to_iso), so text order is time order.

UPDATE sources SET created_at = strftime('%Y-%m-%dT%H:%M:%f', created_at) || '000Z'
WHERE created_at IS NOT NULL AND created_at NOT LIKE '%Z';
UPDATE sources SET updated_at = strftime('%Y-%m-%dT%H:%M:%f', updated_at) || '000Z'
WHERE updated_at IS NOT NULL AND updated_at NOT LIKE '%Z';
UPDATE source_runs SET started_at = strftime('%Y-%m-%dT%H:%M:%f', started_at) || '000Z'
WHERE started_at IS NOT NULL AND started_at NOT LIKE '%Z';
UPDATE source_runs SET finished_at = strftime('%Y-%m-%dT%H:%M:%f', finished_at) || '000Z'
WHERE finished_at IS NOT NULL AND finished_at NOT LIKE '%Z';
UPDATE threads SET first_seen_at = strftime('%Y-%m-%dT%H:%M:%f', first_seen_at) || '000Z'
WHERE first_seen_at IS NOT NULL AND first_seen_at NOT LIKE '%Z';
UPDATE threads SET last_seen_at = strftime('%Y-%m-%dT%H:%M:%f', last_seen_at) || '000Z'
WHERE last_seen_at IS NOT NULL AND last_seen_at NOT LIKE '%Z';
UPDATE events SET created_at = strftime('%Y-%m-%dT%H:%M:%f', created_at) || '000Z'
WHERE created_at IS NOT NULL AND created_at NOT LIKE '%Z';
UPDATE events SET updated_at = strftime('%Y-%m-%dT%H:%M:%f', updated_at) || '000Z'
WHERE updated_at IS NOT NULL AND updated_at NOT LIKE '%Z';
UPDATE items SET published_at = strftime('%Y-%m-%dT%H:%M:%f', published_at) || '000Z'
WHERE published_at IS NOT NULL AND published_at NOT LIKE '%Z';
UPDATE items SET first_seen_at = strftime('%Y-%m-%dT%H:%M:%f', first_seen_at) || '000Z'
WHERE first_seen_at IS NOT NULL AND first_seen_at NOT LIKE '%Z';
UPDATE digests SET created_at = strftime('%Y-%m-%dT%H:%M:%f', created_at) || '000Z'
WHERE created_at IS NOT NULL AND created_at NOT LIKE '%Z';
UPDATE feedback SET created_at = strftime('%Y-%m-%dT%H:%M:%f', created_at) || '000Z'
WHERE created_at IS NOT NULL AND created_at NOT LIKE '%Z';
UPDATE preferences SET updated_at = strftime('%Y-%m-%dT%H:%M:%f', updated_at) || '000Z'
WHERE updated_at IS NOT NULL AND updated_at NOT LIKE '%Z';
UPDATE runs SET started_at = strftime('%Y-%m-%dT%H:%M:%f', started_at) || '000Z'
WHERE started_at IS NOT NULL AND started_at NOT LIKE '%Z';
UPDATE runs SET finished_at = strftime('%Y-%m-%dT%H:%M:%f', finished_at) || '000Z'
WHERE finished_at IS NOT NULL AND finished_at NOT LIKE '%Z';
UPDATE quiz_answers SET created_at = strftime('%Y-%m-%dT%H:%M:%f', created_at) || '000Z'
WHERE created_at IS NOT NULL AND created_at NOT LIKE '%Z';

-- One pipeline run at a time across processes (scheduler, API, one-off commands).
CREATE TABLE run_lease (
  name TEXT PRIMARY KEY,
  holder TEXT NOT NULL,
  expires_at TEXT NOT NULL
);
