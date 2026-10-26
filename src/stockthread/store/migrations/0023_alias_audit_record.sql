-- What an alias stood for, in each of its audit rows (#192): its kind and record id. An admin's alias has no request
-- to trace it by, and a removed alias's row is deleted, so without these the log could not say which record a text
-- matched to while it stood. Ids and codes only: the alias text comes from posts, and the log never holds text
-- (0001). Rows written before this migration have NULL here.

ALTER TABLE audit_log ADD COLUMN alias_kind TEXT CHECK (alias_kind IS NULL OR alias_kind IN ('part', 'location'));
ALTER TABLE audit_log ADD COLUMN alias_record INTEGER CHECK (alias_record IS NULL OR alias_record > 0);
