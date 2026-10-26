# Supported versions

What CI tests on every change, and what the connector does with anything else.

## InvenTree

| InvenTree | API version | Database | Tested |
| --- | --- | --- | --- |
| 1.4.3 | 511 | SQLite | integration tests, against a real server in Docker |
| 1.5.6 | 530 | SQLite | integration tests, against a real server in Docker |
| 1.5.6 | 530 | PostgreSQL | integration tests, against a real server in Docker |

The tested range is API 511 to 530: the current stable release and the previous minor. The connector reads the
server's API version when it starts. On a version outside the range it still posts dry runs, but stock writes stay
off, whatever `writes.enabled` says, and the log names both versions
([Troubleshooting](troubleshooting.md#writes-are-on-but-nothing-is-written)). `stockthread check-setup` reports the server version.

Undo leans on one difference between them: InvenTree 1.5.6 finds the marker of a removal that deleted its stock item,
and 1.4.3 does not. On 1.4.3 the connector may accept the item's 404 in its place.

## Python

Python 3.11 or newer. CI runs the unit tests and the eval corpus on 3.11 and 3.13.

## Slack

The connector uses Bolt for Python (`slack_bolt` 1.x) and connects over Socket Mode by default. The Events API over
HTTP is also supported (README, "Serving over HTTP instead of Socket Mode"). Slack has no versions to pin; the app's
scopes and events are in `manifests/slack-socket.json` and `manifests/slack-http.json`.
