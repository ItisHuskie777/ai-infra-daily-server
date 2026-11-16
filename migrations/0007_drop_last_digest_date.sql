-- The profile no longer carries `last_digest_date`: nothing read it, and whether today's
-- digest is still due comes from the digests table. The profile model rejects unknown
-- keys, so the stored value has to lose the key together with the code.
UPDATE preferences SET value_json = json_remove(value_json, '$.last_digest_date')
WHERE key = 'profile';
