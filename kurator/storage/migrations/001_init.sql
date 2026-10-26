-- Schema from SPEC §1 with revision 2026-08-18 changes (A4, B3, D).

CREATE TABLE person (
  id            INTEGER PRIMARY KEY,
  tg_user_id    INTEGER UNIQUE,
  role          TEXT NOT NULL,
  display_name  TEXT NOT NULL,
  tz            TEXT NOT NULL,
  invite_code   TEXT UNIQUE,
  created_at    TEXT NOT NULL,
  deleted_at    TEXT
);

CREATE TABLE tutor (
  id                    INTEGER PRIMARY KEY,
  person_id             INTEGER NOT NULL REFERENCES person(id),
  subject               TEXT NOT NULL,
  schedule_weekday      INTEGER NOT NULL,
  schedule_time_local   TEXT NOT NULL,
  lesson_duration_min   INTEGER DEFAULT 60,
  active                INTEGER DEFAULT 1
);

CREATE TABLE session (
  id                  INTEGER PRIMARY KEY,
  tutor_id            INTEGER NOT NULL REFERENCES tutor(id),
  scheduled_at_utc    TEXT NOT NULL,
  state               TEXT NOT NULL,
  state_changed_at    TEXT NOT NULL,
  assignment_due_utc  TEXT NOT NULL,
  solution_due_utc    TEXT,
  attempt_no          INTEGER NOT NULL DEFAULT 1,
  cancel_reason       TEXT,
  UNIQUE(tutor_id, scheduled_at_utc)
);

CREATE TABLE batch (
  id            INTEGER PRIMARY KEY,
  session_id    INTEGER NOT NULL REFERENCES session(id),
  kind          TEXT NOT NULL,              -- 'assignment' | 'solution' | 'reference'
  sender_id     INTEGER NOT NULL REFERENCES person(id),
  attempt_no    INTEGER NOT NULL DEFAULT 1,
  status        TEXT NOT NULL,              -- 'open' | 'closed'
  opened_at     TEXT NOT NULL,
  closed_at     TEXT,
  close_reason  TEXT,                       -- 'button' | 'timeout' | 'max_items'
  last_item_at  TEXT,
  prompt_chat_id     INTEGER,               -- message with counter and "Done" button
  prompt_message_id  INTEGER,
  supplements_batch_id INTEGER REFERENCES batch(id)
);

CREATE TABLE item (
  id             INTEGER PRIMARY KEY,
  batch_id       INTEGER NOT NULL REFERENCES batch(id),
  chat_id        INTEGER NOT NULL,
  tg_message_id  INTEGER NOT NULL,
  media_group_id TEXT,
  kind           TEXT NOT NULL,             -- 'text' | 'photo' | 'document'
  text           TEXT,
  file_path      TEXT,
  mime           TEXT,
  received_at    TEXT NOT NULL,
  deleted_at     TEXT,
  UNIQUE(batch_id, chat_id, tg_message_id)
);

CREATE TABLE assignment (
  id           INTEGER PRIMARY KEY,
  session_id   INTEGER NOT NULL UNIQUE REFERENCES session(id),
  batch_id     INTEGER NOT NULL REFERENCES batch(id),
  topics_json  TEXT NOT NULL,
  tasks_json   TEXT NOT NULL,
  tasks_count  INTEGER NOT NULL,
  parse_warnings_json TEXT,
  assignment_text TEXT
);

CREATE TABLE grade (
  id                  INTEGER PRIMARY KEY,
  session_id          INTEGER NOT NULL REFERENCES session(id),
  batch_id            INTEGER NOT NULL REFERENCES batch(id),
  attempt_no          INTEGER NOT NULL,
  model               TEXT NOT NULL,
  score_legibility    INTEGER,
  score_template      INTEGER,
  score_structure     INTEGER,
  score_correctness   INTEGER,
  score_total         INTEGER,
  correctness_source  TEXT,
  confidence          REAL,
  needs_human         INTEGER NOT NULL DEFAULT 0,
  needs_human_reason  TEXT,
  per_task_json       TEXT NOT NULL,
  comment_child       TEXT NOT NULL,
  comment_parent      TEXT NOT NULL,
  raw_json            TEXT NOT NULL,
  created_at          TEXT NOT NULL,
  transcript          TEXT NOT NULL,
  unjustified_leap    INTEGER NOT NULL DEFAULT 0,
  regrade_of          INTEGER REFERENCES grade(id),   -- B3: regrade, old row remains
  superseded_at       TEXT,
  parent_verdict      TEXT,
  parent_verdict_at   TEXT,
  parent_verdict_note TEXT,
  corrected_by        INTEGER REFERENCES person(id),
  corrected_score     INTEGER,
  corrected_note      TEXT,
  corrected_at        TEXT
);

-- A4: deduplication via an explicit key. UNIQUE(person, session, kind) broke
-- on the second attempt and did not work at all for the weekly summary (session_id IS NULL).
CREATE TABLE notification (
  id          INTEGER PRIMARY KEY,
  person_id   INTEGER NOT NULL REFERENCES person(id),
  session_id  INTEGER REFERENCES session(id),
  kind        TEXT NOT NULL,
  dedup_key   TEXT NOT NULL UNIQUE,
  sent_at     TEXT NOT NULL,
  payload     TEXT
);

CREATE TABLE event_log (
  id         INTEGER PRIMARY KEY,
  at         TEXT NOT NULL,
  session_id INTEGER,
  actor      TEXT,
  event      TEXT NOT NULL,
  detail     TEXT
);

CREATE TABLE llm_call (
  id            INTEGER PRIMARY KEY,
  at            TEXT NOT NULL,
  session_id    INTEGER,
  purpose       TEXT NOT NULL,
  model         TEXT NOT NULL,
  input_tokens  INTEGER,
  output_tokens INTEGER,
  cost_usd      REAL,
  latency_ms    INTEGER,
  ok            INTEGER NOT NULL DEFAULT 1,
  error         TEXT
);

CREATE TABLE sheets_queue (
  id          INTEGER PRIMARY KEY,
  session_id  INTEGER NOT NULL REFERENCES session(id),
  queued_at   TEXT NOT NULL,
  done_at     TEXT
);

CREATE INDEX idx_session_state ON session(state);
CREATE INDEX idx_batch_open ON batch(status, session_id);
CREATE INDEX idx_grade_session ON grade(session_id, attempt_no);
