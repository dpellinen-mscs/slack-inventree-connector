"""A connector run that starts an undo and is killed while its write is on its way: run as a process of its own by
`tests/integration/test_apply_server.py`, which kills it with SIGKILL, so nothing it would do afterwards happens.

Usage: crashing_undo.py DB WRITE_URL ITEM PART SOURCE DESTINATION QUANTITY. It undoes request 1 of revision 1 of
the post in `test_apply_server.py`, a transfer of QUANTITY of stock item ITEM from SOURCE to DESTINATION, sending the
undo's write to WRITE_URL (a fault proxy that holds it) and reading from STOCKTHREAD_IT_URL. Prints nothing.
"""

from __future__ import annotations

import os
import sys
from datetime import timedelta
from pathlib import Path

import anyio
from pydantic import SecretStr
from views import picked, view

from stockthread.apply import undo_apply
from stockthread.config import InvenTreeSettings
from stockthread.core.model import Match, MatchRule, MatchStatus, PostKey, RequestKey
from stockthread.inventree import InvenTreeReader
from stockthread.store import Store
from stockthread.writer import InvenTreeWriter

OWNER = "crashing-run"
KEY = RequestKey(PostKey("T0ACME001", "C0ACME001", "1700000000.000100"), 1, 1)


async def main(db: str, write_url: str, item: int, part: int, source: int, destination: int, quantity: str) -> None:
    movement = view(
        lines=(("Disc 40", quantity),),
        stock=(picked((item, quantity)),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, part),),
        source=Match(MatchStatus.MATCHED, MatchRule.EXACT, source),
        destination=Match(MatchStatus.MATCHED, MatchRule.EXACT, destination),
    )
    token = SecretStr(Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip())
    store = Store.open(Path(db))
    store.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    reads = InvenTreeSettings(url=os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=60)
    writes = InvenTreeSettings(url=write_url, timeout_seconds=60)
    async with InvenTreeReader(reads, token) as reader, InvenTreeWriter(writes, token) as writer:
        await undo_apply(
            KEY, movement, store=store, reader=reader, writer=writer, lock=anyio.Lock(), lock_owner=OWNER,
            user="U0DAN0001",
        )  # fmt: skip


if __name__ == "__main__":
    db, url, item, part, source, destination, quantity = sys.argv[1:]
    anyio.run(main, db, url, int(item), int(part), int(source), int(destination), quantity)
