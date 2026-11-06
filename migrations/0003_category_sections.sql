-- Digest sections became topic categories (models, research, infrastructure, agents,
-- business, policy, other). Placements of earlier digests keep their events and topics;
-- their section, which has no counterpart, becomes 'other'.
UPDATE digest_events SET section = 'other'
WHERE section IN ('top', 'threads', 'voices', 'papers', 'community');
