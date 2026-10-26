-- Quiet hours no longer discard messages (case from 28.08.2026).
--
-- notify() logged "deferred … until 08:00" when a message fell during quiet hours
-- and returned: there was no queue or retry, so the message
-- disappeared forever. The student never learned that an assignment was withdrawn or that there was a new one.
--
-- The message is now stored here with status='deferred' and a not_before time,
-- and the scheduler sends it once it's due. The text and buttons are stored alongside it: when
-- it's time to send, there's no way to rebuild them.
ALTER TABLE notification ADD COLUMN status TEXT;
ALTER TABLE notification ADD COLUMN not_before TEXT;
ALTER TABLE notification ADD COLUMN chat_id INTEGER;
ALTER TABLE notification ADD COLUMN buttons_json TEXT;

UPDATE notification SET status = 'sent' WHERE status IS NULL;
