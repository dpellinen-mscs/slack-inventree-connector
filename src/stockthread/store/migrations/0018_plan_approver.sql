-- Who approved a plan revision by an Apply click (design section 6): that approver's Slack user id, set as the plan's
-- requests are approved together (`Store.approve_plan`). NULL when the policy approved it, or nothing did. The gate is
-- judged again before every write with this approver, so the click counts only while they are still an approver.

ALTER TABLE plans ADD COLUMN approved_by TEXT;
