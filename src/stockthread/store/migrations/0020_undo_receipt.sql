-- A receipt also reports an undo (design section 7, "Undo"): `requests.receipt_state` may now be `undone` or
-- `undo_failed` as well as the three apply outcomes. SQLite cannot change a column's CHECK, so the column is copied
-- into a new one with the wider CHECK, dropped, and the new one takes its name. `Store._set_state` now also forgets
-- the reported state when an undo starts, so an undo that ends `verify_failed` is reported even when its apply was.

ALTER TABLE requests ADD COLUMN receipt_state_new TEXT CHECK (
    receipt_state_new IS NULL
    OR receipt_state_new IN ('verified', 'verify_failed', 'failed', 'undone', 'undo_failed')
);
UPDATE requests SET receipt_state_new = receipt_state;
ALTER TABLE requests DROP COLUMN receipt_state;
ALTER TABLE requests RENAME COLUMN receipt_state_new TO receipt_state;

-- As in 0019: an undo concluded before receipts reported undos was never shown one, and its thread has moved on.
UPDATE requests SET receipt_state = state WHERE state IN ('undone', 'undo_failed');
-- An undo still in flight started before its start forgot the reported state: forget it now, so its outcome is
-- reported even when it is the state the last receipt reported (`verify_failed` again).
UPDATE requests SET receipt_state = NULL WHERE state IN ('undoing', 'undo_unknown');
