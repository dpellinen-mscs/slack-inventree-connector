# Security policy

stockthread writes to an inventory system on the strength of chat
messages, so a flaw in it can change stock without anyone noticing. Please
report such flaws privately first.

## Reporting a vulnerability

Use GitHub's private vulnerability reporting: open the repository's
**Security** tab and choose **Report a vulnerability**. Only the
maintainers can see the report.

Please do not open a public issue, pull request or discussion for a
vulnerability before it is fixed.

A useful report says:

- what an attacker can do, and what they need first (a Slack account in a
  watched channel, a network position, access to the host);
- the steps or a synthetic message that shows it, never real inventory
  data, customer details or live tokens;
- the version or commit you tested.

This is a small project maintained in spare time. You will get an answer
as soon as a maintainer can look at the report. A fix is released before
the details are made public, and you are credited unless you ask not to
be.

## What counts

Anything that breaks a safety rule of the [design](docs/design.md) or a
control of the [threat model](docs/threat-model.md), for example:

- a stock write without the write switch, the policy and the deterministic
  checks all allowing it, or a write that model confidence alone allowed;
- one Slack post applied more than once, or an undo that changes stock it
  should not touch;
- a Slack request accepted without a valid signature, or an action from a
  user who is not allowed to approve;
- message text, tokens or secrets in logs, InvenTree notes or the audit
  trail where the docs say they are not kept;
- instructions inside a message that change what the connector does.

Out of scope: an attacker who already has a shell or file access on the
connector's host (see the threat model), and flaws in Slack, InvenTree or
an LLM provider themselves. Please report those to their projects.

## Supported versions

Before 1.0, only the latest commit on `main` gets security fixes.
