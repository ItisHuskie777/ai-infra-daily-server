-- Topics the ranking model assigned to each placed event, so feedback on the event can
-- adjust the owner's topic weights.
ALTER TABLE digest_events ADD COLUMN topics_json TEXT NOT NULL DEFAULT '[]';

-- One answer per digest and question; a second click on the same question is ignored.
CREATE TABLE quiz_answers (
  digest_id TEXT NOT NULL REFERENCES digests(id),
  question TEXT NOT NULL,
  answer TEXT NOT NULL,
  created_at TEXT NOT NULL,
  PRIMARY KEY (digest_id, question)
);
