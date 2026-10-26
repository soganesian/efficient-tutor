-- Lesson in archive: hidden from reports, dashboard, statistics, and /retake.
--
-- On 02.10.2026, the parent asked to remove the lessons from 25.08–04.09: the bot was still
-- being configured then, and the data for them is incorrect (submission linked to the wrong lesson, "Done"
-- instead of a solution). They cannot be deleted (CLAUDE.md §8) — rows and files remain,
-- and list queries skip them.
ALTER TABLE session ADD COLUMN archived_at TEXT;
ALTER TABLE session ADD COLUMN archive_reason TEXT;
