-- What recovery concluded about an unknown attempt's marker (design section 7, "Unknown outcomes and recovery";
-- review of PR #100): 1 found, 0 not found, NULL not recovered. The marker check of a later apply or undo reads it:
-- a marker found after recovery concluded "not found" is a write that landed late, which the request's state no
-- longer accounts for (an undo built on "not found, changed" would reverse the stock twice).
--
-- No attempt was recovered before this migration: recovery was not built yet.

ALTER TABLE request_attempts ADD COLUMN marker_found INTEGER CHECK (marker_found IS NULL OR marker_found IN (0, 1));

CREATE TRIGGER attempts_recovered_once BEFORE UPDATE OF marker_found ON request_attempts
WHEN OLD.marker_found IS NOT NULL OR NEW.marker_found IS NULL OR OLD.outcome IS NOT 'unknown'
BEGIN SELECT RAISE(ABORT, 'recovery concludes once, about an attempt whose outcome is unknown'); END;
CREATE TRIGGER attempts_born_unrecovered BEFORE INSERT ON request_attempts
WHEN NEW.marker_found IS NOT NULL
BEGIN SELECT RAISE(ABORT, 'an attempt starts unrecovered'); END;
