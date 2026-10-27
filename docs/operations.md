# Operations: the database, backups and upgrades

The connector keeps everything it knows in one SQLite file: the Slack events it accepted, each
post's dry runs, the questions and answers, plans and requests with their states, receipts, learned
aliases, and the append-only audit log. InvenTree holds the stock. This page covers how to back the
file up, restore it, and upgrade the connector over it.

## Where the database is

`storage.database` in `stockthread.toml` (default `stockthread.db`). A relative path is taken from the
directory the connector is started in, which in the quickstart is the checkout that holds
`stockthread.toml`.

- The file is created readable by its owner only (mode 0600), because it holds every accepted post in
  full. An existing file keeps its mode: keep a restored copy at 0600 too.
- It runs in WAL mode. While the connector runs, `stockthread.db-wal` and `stockthread.db-shm` sit
  next to it, and the newest changes may be in the `-wal` file only. **Copying `stockthread.db` alone
  while the connector runs can give a backup without its newest changes, or a broken one.**
- Message text is blanked `storage.retention_days` days after a post is done with. Ids, states and the
  audit log stay. A backup holds whatever text the live file held when it was made.
- One connector runs on one database: a second one started on the same file refuses to run while the
  first one's heartbeat is fresh.

## Backup

Use SQLite's online backup. It gives a consistent copy, the `-wal` file included, while the
connector keeps running. From the directory that holds the database:

```sh
(umask 077; .venv/bin/python -c "import sqlite3; sqlite3.connect('file:stockthread.db?mode=ro', uri=True).backup(sqlite3.connect('stockthread-backup.db'))")
```

`mode=ro` makes a wrong path fail ("unable to open database file") instead of quietly backing up a
new, empty database. `umask 077` keeps the copy readable by you only. Store backups like the database
itself: they hold Slack message text. If you have the `sqlite3` command-line tool,
`sqlite3 stockthread.db ".backup stockthread-backup.db"` does the same, but it does not fail on a
wrong path.

Check a backup before you rely on it. This prints `ok` and the number of posts it holds:

```sh
.venv/bin/python -c "import sqlite3; c = sqlite3.connect('file:stockthread-backup.db?mode=ro', uri=True); print(c.execute('PRAGMA integrity_check').fetchone()[0], c.execute('SELECT count(*) FROM posts').fetchone()[0])"
```

A count of 0 on a connector that has answered posts means you backed up the wrong file.

Alternatively, stop the connector, then copy `stockthread.db` together with any `-wal` and `-shm`
files next to it.

`stockthread aliases export` prints the learned aliases as JSON. That export is readable without the
connector, but it is not a backup of the database.

## Restore

1. Stop the connector.
2. Move `stockthread.db` and any `stockthread.db-wal` and `stockthread.db-shm` next to it out of the
   way. A `-wal` file left from the old database must not meet the restored one.
3. Copy the backup to `stockthread.db`, then run `chmod 600 stockthread.db`.
4. Set `writes.enabled = false` in `stockthread.toml` (see below for why).
5. Start the connector. Let it catch up, then check the posts it answers again (below) against
   InvenTree or the old database's `trail`.
6. Set `writes.enabled` back to what it was and restart the connector.

What happens after a restore:

- **Posts made after the backup are read and answered again.** At start, catch-up reads each channel
  from where the backup last left it, up to `slack.catch_up_hours` back (72 by default). A post made
  after the backup is not in the restored database, so it gets a new dry run and a new reply in its
  thread, with the review reason `late_ingest`. That reason keeps it from being applied
  automatically, but **an Apply click on it writes the movement again**, even if it was already
  applied before the restore: its new requests carry new markers, so the marker check (below) cannot
  see the earlier write. Do not click Apply on any of these posts until you have checked that it did
  not move stock already. With `writes.enabled = false`, as in step 4, nothing can be written while
  you check.
- Stock moved after the backup was made is not undone. InvenTree keeps it. The restored database just
  does not know about those writes: `trail` won't show them, and an Undo click on their receipts
  changes nothing. Undo them in InvenTree if needed.
- A request the backup still shows as approved or in flight is checked against InvenTree before
  anything is written. Every write leaves its marker in the notes of the stock adjustment it makes,
  and the marker check finds a write that already happened ([design](design.md) section 7). So a
  request the backup already holds is not written twice. This protects only requests in the backup,
  not the posts above.
- Plans in the backup that nobody acted on have most likely expired by now, and an Apply click on
  them is refused as expired.
- Text the live file had already blanked comes back from the backup. The next hourly retention pass
  blanks it again once it is old enough.
- The instance lock in the backup names the old process. Its heartbeat is stale, so the new start
  takes the lock over.

## Upgrade

1. Make a backup (above).
2. Stop the connector.
3. Install the new version into the same virtual environment. From an updated checkout:
   `.venv/bin/pip install .` (`.venv/bin/stockthread --version` prints the version installed)
4. Read the new [CHANGELOG](../CHANGELOG.md) entries for settings that changed, then run
   `.venv/bin/stockthread check-config`.
5. Start the connector. It upgrades the database schema at start, one migration at a time. Each
   migration runs in its own transaction, so a migration that fails is rolled back and the connector
   stops with the error. Fix the cause and start it again.

Until the connector has started once on the new version, `stockthread trail` and the alias commands
refuse to read the old schema ("start the connector once to upgrade it").

**Going back to an older version.** Migrations only go forward. An older version refuses to open a
database whose schema is newer than it knows ("the database has schema version N; this version knows
M"). To go back, stop the connector, install the older version, and restore the backup you made in
step 1, following the Restore steps (writes off until the posts answered again are checked).
Anything recorded since that backup is lost from the database, as described under Restore.
