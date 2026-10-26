-- Two lessons per week. The student has math on Tuesdays and Fridays, while
-- schedule_weekday held exactly one day, and there was no way to express this schedule:
-- adding the tutor twice doesn't work, tutor_by_person() returns
-- one row, and the batch handler would always land on the same lesson.
--
-- The old column stays in place (CLAUDE.md §8, don't delete anything) and
-- serves as the source for populating the new one.
ALTER TABLE tutor ADD COLUMN schedule_weekdays TEXT;

UPDATE tutor
   SET schedule_weekdays = '[' || CAST(schedule_weekday AS TEXT) || ']'
 WHERE schedule_weekdays IS NULL;
