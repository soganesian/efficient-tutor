-- Different lesson times on different weekdays (13.09.2026).
--
-- schedule_time_local held one time for all days, but the student has math
-- on Tuesday at 15:00 and Friday at 14:30 Moscow time. There was no way to express
-- a schedule like this. Now the schedule is a list of slots:
--   [{"weekday": 1, "time": "15:00", "duration_min": 60}, ...]
-- The old columns remain in place (CLAUDE.md §8) and serve as the source for
-- slots_of() if the list has not been populated yet.
ALTER TABLE tutor ADD COLUMN schedule_slots TEXT;

-- The duration is fixed on the lesson rather than taken from the tutor: after
-- being rescheduled, the lesson has a life of its own, and the slot it came from
-- could have changed.
ALTER TABLE session ADD COLUMN duration_min INTEGER;

-- Tutor confirmation that "the lesson took place" before requesting materials (SPEC §3a).
-- NULL — not asked, no response, or lesson rescheduled. Otherwise, 'yes' or 'no'.
ALTER TABLE session ADD COLUMN lesson_confirmed TEXT;
ALTER TABLE session ADD COLUMN confirm_asked_at TEXT;

-- Lesson rescheduled by the tutor. origin_slot_utc is the schedule slot the lesson
-- came from: without it, the scheduler would create a new lesson in the freed slot
-- and cancel the rescheduled one as "schedule_changed" (B8).
ALTER TABLE session ADD COLUMN origin_slot_utc TEXT;
ALTER TABLE session ADD COLUMN moved_at TEXT;
ALTER TABLE session ADD COLUMN moved_from_utc TEXT;

UPDATE session SET origin_slot_utc = scheduled_at_utc WHERE origin_slot_utc IS NULL;
