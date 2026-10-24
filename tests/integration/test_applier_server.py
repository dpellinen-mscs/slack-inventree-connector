"""The worker's apply step (`applier`) against a real server: a reply's plan, approved by the policy with the server's
own version check, written once and read back with the connector's own reader and writer."""

from __future__ import annotations

import json
import os
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

import anyio
import pytest
from pydantic import SecretStr
from views import picked, view

from stockthread.applier import GateNow, approve_by_policy, work_request
from stockthread.config import InvenTreeSettings, parse_settings
from stockthread.core.matching import Catalog
from stockthread.core.model import Extraction, Match, MatchRule, MatchStatus, MovementType, PostKey, RequestKey
from stockthread.core.requests import RequestState
from stockthread.gate import Policy, Verdict
from stockthread.inventree import InvenTreeReader, check_version
from stockthread.plan_record import plan_of
from stockthread.run import inventree_steps, policy_approver
from stockthread.slack.replies import MovementView
from stockthread.store import DryRunOutcome, NewPlan, Store
from stockthread.worker import _record
from stockthread.writer import InvenTreeWriter

if TYPE_CHECKING:
    from conftest import Scratch

OWNER = "worker-it"
SAM = "U0SAM0001"
POST = PostKey("T0ACME001", "C0ACME001", "1700000000.000100")
KEY = RequestKey(POST, 1, 1)


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    opened = Store.open(tmp_path / "stockthread.db")
    opened.acquire_lock(OWNER, stale_after=timedelta(seconds=60))
    opened.place_message(OWNER, POST, user_id=SAM, thread_ts=None, window_seconds=5)
    yield opened
    opened.close()


def token() -> SecretStr:
    return SecretStr(Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip())


def server() -> InvenTreeSettings:
    return InvenTreeSettings(url=os.environ["STOCKTHREAD_IT_URL"], timeout_seconds=10)


def take_to_truck(scratch: Scratch, pk: int, quantity: str) -> MovementView:
    """Sam takes `quantity` of stock item `pk` from the scratch parent location to his truck (the other one)."""
    return view(
        lines=(("Disc 40", quantity),),
        stock=(picked((pk, quantity)),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.part),),
        source=Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.parent),
        destination=Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.other),
    )


def reply(store: Store, movement: MovementView) -> None:
    stored = _record(Extraction("rules", (movement.movement,)), [movement], late=False, poster=SAM, messages=(),
                     max_quantity=500)  # fmt: skip
    result: dict[str, Any] = json.loads(json.dumps(stored))
    made = plan_of(result)
    assert made is not None
    store.finish_dry_run(OWNER, POST, DryRunOutcome.REPLIED, "1700000001.000100", result=result, plan=NewPlan(*made))


@pytest.mark.anyio
async def test_a_clear_take_out_is_approved_by_the_policy_written_once_and_verified(
    store: Store, scratch: Scratch
) -> None:
    item = scratch.stock(5)
    reply(store, take_to_truck(scratch, item["pk"], "2"))
    async with InvenTreeReader(server(), token()) as reader, InvenTreeWriter(server(), token()) as writer:
        now = GateNow(
            Policy(
                True, frozenset({MovementType.TAKE_TO_VEHICLE}), frozenset(), check_version(await reader.server_info())
            ),
            {SAM: scratch.other},
        )
        judgement = approve_by_policy(store, OWNER, POST, now)
        assert judgement is not None
        assert judgement.verdict is Verdict.ALLOWED, judgement.why
        assert store.requests_to_work() == [KEY]
        lock = anyio.Lock()
        applied = await work_request(
            KEY,
            store=store,
            reader=reader,
            writer=writer,
            lock=lock,
            lock_owner=OWNER,
            gate_now=lambda: now,
            expired=lambda _: False,
            settle=timedelta(seconds=60),
        )
    assert applied.problems == (), applied.problems
    assert applied.state is RequestState.VERIFIED
    assert store.requests_to_work() == []
    moved = scratch.api.items(scratch.part, scratch.other)
    assert sum(Decimal(str(row["quantity"])) for row in moved) == Decimal(2)


def test_the_run_loops_steps_write_once_read_back_and_undo_on_the_real_server(store: Store, scratch: Scratch) -> None:
    """`run.policy_approver` and `run.inventree_steps`, as `stockthread run` gives them to the worker with writes on: a
    fresh client and a fresh version check per call, the vehicles from the catalog."""
    item = scratch.stock(5)
    movement = take_to_truck(scratch, item["pk"], "2")
    reply(store, movement)
    settings = parse_settings(
        {
            "slack": {"workspace_id": POST.workspace_id, "app_id": "A0ACME001", "channels": [POST.channel_id]},
            "inventree": {"url": os.environ["STOCKTHREAD_IT_URL"], "timeout_seconds": 10},
            "writes": {"enabled": True},
            "apply": {"settle_seconds": 60},
        },
        {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic",
         "INVENTREE_TOKEN": token().get_secret_value()},
    )  # fmt: skip
    vehicles = Catalog((), (), vehicles={SAM: scratch.other})

    def soon() -> datetime:  # the post's plan expires a day after it
        return datetime.fromtimestamp(1700000300, UTC)

    step, undo = inventree_steps(settings, lambda: vehicles, store, OWNER, beat=lambda: None, clock=soon)
    approve = policy_approver(settings, lambda: vehicles, store, OWNER, beat=lambda: None)  # the real version check
    judgement = approve(POST)
    assert judgement is not None
    assert judgement.allowed, judgement.why
    applied = step(KEY)
    assert applied.state is RequestState.VERIFIED, applied
    assert sum(Decimal(str(row["quantity"])) for row in scratch.api.items(scratch.part, scratch.other)) == Decimal(2)
    undone = undo(KEY, movement, SAM)
    assert undone.state is RequestState.UNDONE, undone
    assert sum(Decimal(str(row["quantity"])) for row in scratch.api.items(scratch.part, scratch.other)) == Decimal(0)
