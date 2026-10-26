-- Hints for the student on their current homework (SPEC §4b).
--
-- Parent's requirement, 09.10.2026: a hint points the way, it is not a solution,
-- only for current homework, no more than 3 per homework, across all tasks.
-- The limit is counted using rows in this table, so only a delivered
-- hint is recorded: an AI failure does not use up an attempt.
CREATE TABLE hint (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES session(id),
  task_no     INTEGER NOT NULL,             -- 0 if the homework is not split into tasks
  attempt_no  INTEGER NOT NULL,             -- which attempt they asked on
  hint_text   TEXT NOT NULL,
  model       TEXT NOT NULL,
  raw_json    TEXT NOT NULL,
  created_at  TEXT NOT NULL
);

CREATE INDEX idx_hint_session ON hint(session_id, task_no);
