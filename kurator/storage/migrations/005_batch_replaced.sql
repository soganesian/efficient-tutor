-- Batch re-upload: the tutor sent the wrong files (SPEC §4.6).
--
-- We don't delete anything (CLAUDE.md §8): the old batch remains with its items
-- and files on disk, but is marked with replaced_at and is no longer considered "latest".
-- latest_batch() skips it, so the new one is used for grading, /files, and the dashboard.
ALTER TABLE batch ADD COLUMN replaced_at TEXT;
