-- The receipt of a plan revision (design section 7, step 6): the ts of the thread reply that reported what its writes
-- did, set once it is posted (`Store.receipt_posted`). NULL until then; a plan whose requests have all concluded and
-- that has no receipt is due one (`Store.receipts_due`). A crash between the post and this update posts the receipt
-- again at restart: a duplicate beats a silent drop.

ALTER TABLE plans ADD COLUMN receipt_ts TEXT CHECK (receipt_ts IS NULL OR receipt_ts <> '');
