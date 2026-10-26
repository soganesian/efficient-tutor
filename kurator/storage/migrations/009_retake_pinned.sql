-- Batch pinned to a lesson using the /retake command (SPEC §4a).
--
-- On 15.09.2026, the student resubmitted one photo, and the bot would no longer
-- accept the next retake: the lesson was stuck in GRADED, and the batch in the regrading window would be considered
-- overdue. The student's choice is an explicit retake, the regrading window does not
-- apply to it. The bot loses the pin from memory on restart, so the flag
-- is set on the batch when it is opened.
ALTER TABLE batch ADD COLUMN retake_pinned INTEGER NOT NULL DEFAULT 0;
