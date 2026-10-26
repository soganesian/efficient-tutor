-- Linking by @username: the bot cannot look up a user_id by name, but every
-- incoming update includes a username — that's enough to link a person
-- to their account without invitation codes.
ALTER TABLE person ADD COLUMN username TEXT;
CREATE UNIQUE INDEX idx_person_username ON person(username) WHERE username IS NOT NULL;
