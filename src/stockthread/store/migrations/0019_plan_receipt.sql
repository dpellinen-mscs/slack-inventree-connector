-- The receipt of a plan revision (design section 7, step 6). `requests.receipt_state` is the state of the request the
-- last receipt of its plan reported, and `plans.receipt_ts` the ts of that receipt in the thread; both are set together
-- once a receipt is posted (`Store.receipt_posted`). A plan is due a receipt when no step of the protocol is due or in
-- flight for any of its requests and one of them concluded a write in a state no receipt reported
-- (`Store.receipts_due`), so a write a person tried again is reported again. A crash between the post and the record
-- posts the receipt again at restart: a duplicate beats a silent drop.

ALTER TABLE plans ADD COLUMN receipt_ts TEXT CHECK (receipt_ts IS NULL OR receipt_ts <> '');
ALTER TABLE requests ADD COLUMN receipt_state TEXT CHECK (
    receipt_state IS NULL OR receipt_state IN ('verified', 'verify_failed', 'failed')
);

-- A write concluded before receipts existed was never shown one, and its thread has moved on: it is not reported now.
UPDATE requests SET receipt_state = state WHERE state IN ('verified', 'verify_failed', 'failed');
