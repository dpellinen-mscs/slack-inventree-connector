# 0013: HTTP mode is served by waitress, in the connector's own process

- Status: Accepted
- Date: 2026-10-01
- Decided by: agent (FYI to Dan)

## Context

Socket Mode is the default transport (design section 4). Some teams prefer
the Events API over HTTP: no long-lived WebSocket, and the usual reverse
proxy in front. Bolt for Python has a WSGI adapter, but its own
`app.start()` is a development server. `stockthread run` keeps a SQLite
instance lock, the durable queue and the worker in one process, so a server
that forks worker processes (gunicorn, uWSGI) would split that process and
fight over the lock.

## Decision

- **waitress** serves Bolt's WSGI adapter on a thread inside the `run`
  process, with 4 request threads. It is pure Python, maintained by the
  Pylons project, runs on every platform the connector supports, and needs
  no build step. It is a runtime dependency (`waitress>=3.0.2,<4`).
- **Settings.** `[slack.http]` with `host` (default `127.0.0.1`), `port`
  (default `3000`) and `path` (default `/slack/events`). Events and button
  clicks share the one path. The default host takes requests from the same
  machine only: TLS is the reverse proxy's job, and Slack only calls https
  URLs.
- **Signatures.** A small WSGI wrapper checks the signature of every POST
  on the raw bytes before Bolt parses it, and refuses with 401 without
  logging anything: Bolt itself logs the whole body of a request it
  refuses, and turns a body it cannot parse into a traceback. Bolt then
  checks again.
  This is always on (`build_app` has no switch for it), and HTTP
  mode refuses to start without `SLACK_SIGNING_SECRET`.
- **Limits.** A body over 4 MiB is refused with 413 before Bolt reads it.
  A connection idle for 15 s is closed.
  Only POST to the path reaches Bolt; anything else is a 404. Proxy headers
  from untrusted peers are cleared, and the connector uses no client address.
- **Lifecycle.** The listener binds after the startup checks, like the
  Socket Mode connection. A port that cannot be bound stops startup with
  exit code 2 and names only the address and the errno. On stop, the
  serving thread ends its own loop and closes its sockets itself: closing
  them from another thread races waitress's `select`. Answers already in
  hand get 3 s to go out. If the serving thread dies by itself, `run`
  stops with exit code 1, so a supervisor starts it again.

## Consequences

- One more dependency, small and pure Python. A second manifest
  (`manifests/slack-http.json`) with a placeholder URL.
- The admin runs a reverse proxy with TLS. The README names the request URL
  and the setting for a container (`host = "0.0.0.0"`), and does not teach
  proxy setup.
- Revisit if the connector ever runs several processes, or if an ASGI stack
  replaces Bolt's synchronous handlers.
