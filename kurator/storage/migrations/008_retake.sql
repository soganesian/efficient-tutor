-- Retake: the student submits work after the regular window has closed (SPEC §4a).
--
-- Parent request 13.09.2026: a retake can be submitted at any time, even a month later, initiated by
-- the student. There are then two grades for the lesson: one for homework in the
-- regular window and a separate one for the retake, the weekly average score is calculated from
-- homework, with retakes listed on their own line.
ALTER TABLE batch ADD COLUMN is_retake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE batch ADD COLUMN retake_days INTEGER;

-- Duplicated in grade so reports and the dashboard read from a single row.
ALTER TABLE grade ADD COLUMN is_retake INTEGER NOT NULL DEFAULT 0;
ALTER TABLE grade ADD COLUMN retake_days INTEGER;
ALTER TABLE grade ADD COLUMN retake_no INTEGER NOT NULL DEFAULT 0;

ALTER TABLE session ADD COLUMN retake_no INTEGER NOT NULL DEFAULT 0;

-- Original homework deadline. solution_due_utc shifts on the second attempt,
-- while the reference point for "how many days have passed" must remain fixed.
ALTER TABLE session ADD COLUMN homework_due_utc TEXT;

UPDATE session SET homework_due_utc = solution_due_utc WHERE homework_due_utc IS NULL;
