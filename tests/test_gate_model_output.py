"""The model cannot authorize a write (design section 6, ADR 0004; ROADMAP M3 DoD): every corpus post runs through
the worker as `stockthread run` builds it with writes on, and the model's recorded answer is varied. Stripping the
model's flags never turns a post the policy does not approve into one it approves, and an answer that names a
quantity, a part or a place the post does not state is never approved by the policy.

The model's answer has no score to vary (`llm.extract.OUTPUT_SCHEMA`): what it can steer the gate by is what it
omits and what it makes up. What it can omit is its flags, and a source the post states, which a default source would
otherwise fill. The model reads every post here, not only those the rules decline, so the gate does not lean on the
rules having read a post first. Each post's catalog knows its words as an admin's aliases, and every place holds
plenty of stock, so that only the rules and the gate stand in the way.
Take-outs have a default source (`policy.default_source`, ADR 0020), as most of them state none.
"""

from __future__ import annotations

import copy
import dataclasses
import json
import shutil
from collections.abc import Callable, Iterator, Mapping, Sequence
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from corpus import EVAL, cases, eval_catalog

from stockthread.applier import GateNow, approve_by_policy
from stockthread.catalog import assign_default_sources
from stockthread.config import Settings, parse_settings
from stockthread.core import extract_rules
from stockthread.core.matching import Alias, Catalog
from stockthread.core.model import Extraction, MessageInput, PostKey
from stockthread.core.resolve import STATUS_OK, StockItem
from stockthread.gate import Judgement, Policy, Verdict
from stockthread.inventree import DEPLETING_MARKER_FOUND_FROM, ServerInfo, VersionCheck
from stockthread.llm.extract import LLMExtractor
from stockthread.store import Store
from stockthread.worker import Worker

OWNER = "instance-a"
WORKSPACE, APP = "T0ACME001", "A0ACME001"
LUIS = "U0LUIS0005"
TESTED = VersionCheck(ServerInfo("1.5.6", DEPLETING_MARKER_FOUND_FROM), True, "tested")
ENV = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"}
RECORDING = json.loads((EVAL / "recorded" / "claude-haiku-4.5.json").read_text(encoding="utf-8"))
ANSWERS = {answer["id"]: answer for answer in RECORDING["answers"]}
CASES = [case for case in cases() if ANSWERS.get(case["id"], {}).get("output") is not None]
CHANNELS = sorted({case["post"]["channel"] for case in CASES})
CATALOG, _IPNS, _PATHS = eval_catalog()
PART = {ipn: pk for pk, ipn in _IPNS.items()}
LOCATION = {path: pk for pk, path in _PATHS.items()}
DEFAULT_SOURCE = {"take_to_vehicle": "Warehouse/Main Stock", "take_to_site": "Warehouse/Main Stock"}


def knowing(case: Mapping[str, Any]) -> Catalog:
    """The eval catalog with an admin's alias for every text the case's expected movements match, so that no match
    keeps a post from the gate: the gate and the rules alone stand between the model and an automatic write."""
    parts: list[Alias] = []
    locations: list[Alias] = []
    for movement in case["expected"]["movements"]:
        for item in movement["line_items"]:
            match = item.get("match") or {}
            if match.get("status") == "matched" and item["item_text"]:
                parts.append(Alias(item["item_text"], PART[match["part"]]))
        for side in ("source", "destination"):
            match = movement.get(f"{side}_match") or {}
            if match.get("status") == "matched" and movement[f"{side}_text"]:
                locations.append(Alias(movement[f"{side}_text"], LOCATION[match["location"]]))
    known = dataclasses.replace(
        CATALOG,
        part_aliases=CATALOG.part_aliases + tuple(parts),
        location_aliases=CATALOG.location_aliases + tuple(locations),
    )
    return assign_default_sources(known, DEFAULT_SOURCE)[0]


def settings() -> Settings:
    """Writes on, with the default policy and a default source for take-outs."""
    data = {
        "slack": {"workspace_id": WORKSPACE, "app_id": APP, "channels": CHANNELS, "approvers": [LUIS]},
        "inventree": {"url": "http://inventree.local:8000"},
        "writes": {"enabled": True},
        "policy": {"default_source": DEFAULT_SOURCE},
    }
    return parse_settings(data, ENV)


class FakeSlack:
    def __init__(self) -> None:
        self.posted: list[dict[str, Any]] = []

    def chat_postMessage(self, **kwargs: Any) -> dict[str, Any]:
        self.posted.append(kwargs)
        return {"ok": True, "ts": f"1799999999.{len(self.posted):06d}"}


class Clock:
    def __init__(self) -> None:
        self.now = datetime.fromtimestamp(1790000000, UTC)

    def __call__(self) -> datetime:
        return self.now


class Answer:
    """A provider that answers with one fixed output."""

    model = str(RECORDING["model"])

    def __init__(self, output: str) -> None:
        self.output = output

    def complete(self, system: str, user: str, schema: Mapping[str, Any]) -> str:
        return self.output


def plenty(pairs: Sequence[tuple[int, int]]) -> dict[tuple[int, int], list[StockItem]]:
    """One stock item of 50 for every part at every place, so that stock never stands in the way."""
    return {pair: [StockItem(10_000 + n, Decimal(50), STATUS_OK, Decimal(0))] for n, pair in enumerate(pairs)}


def event(post: Mapping[str, Any], message: Mapping[str, Any], number: int) -> dict[str, Any]:
    body: dict[str, Any] = {
        "type": "message",
        "channel": post["channel"],
        "user": message.get("user", post["user"]),
        "text": message.get("text", ""),
        "ts": message["ts"],
    }
    if "thread_ts" in message:
        body["thread_ts"] = message["thread_ts"]
    if message.get("files"):
        body["subtype"] = "file_share"
        body["files"] = [{"id": file["id"], "mimetype": file.get("mimetype")} for file in message["files"]]
    if message.get("is_bot"):
        body["bot_id"] = "B0OTHERBOT"
    return {"type": "event_callback", "team_id": WORKSPACE, "api_app_id": APP, "event_id": f"Ev{number}", "event": body}


STEPS = (1, 15, 30, 45, 400, 415, 430)
"""Seconds after the post's last message at which the worker steps: past the grace, the model's turn and the wait
for a thread's root."""


@pytest.fixture(scope="module")
def migrated(tmp_path_factory: pytest.TempPathFactory) -> Path:
    """An empty database at the newest schema, copied for each run: migrating takes most of a run's time."""
    path = tmp_path_factory.mktemp("migrated") / "stockthread.db"
    Store.open(path).close()
    return path


def verdict(empty: Path, path: Path, case: Mapping[str, Any], output: dict[str, Any]) -> Verdict | None:
    """What the policy judged the post's plan, run as the worker runs it with the model answering `output`, or
    None when the reply made no plan. `empty` is copied to `path` for the run's database."""
    shutil.copyfile(empty, path)
    clock = Clock()
    store = Store.open(path, clock=clock)
    try:
        store.acquire_lock(OWNER, stale_after=timedelta(days=365))
        return _verdict(store, clock, case, json.dumps(output))
    finally:
        store.close()


def _verdict(store: Store, clock: Clock, case: Mapping[str, Any], output: str) -> Verdict | None:
    current = settings()
    catalog = knowing(case)
    judged: list[Judgement | None] = []
    now = GateNow(Policy.of(current, TESTED), dict(catalog.vehicles), dict(catalog.default_sources))

    def approve(post: PostKey) -> Judgement | None:
        judged.append(approve_by_policy(store, OWNER, post, now))
        return judged[-1]

    worker = Worker(
        current, store, OWNER, FakeSlack(), lambda: catalog, clock, stock=plenty,  # type: ignore[arg-type]
        llm=LLMExtractor(Answer(output)), approve=approve,
    )  # fmt: skip
    post = case["post"]
    for number, message in enumerate(post["messages"], start=1):
        assert store.enqueue_event(f"Ev{number}:event", "event", json.dumps(event(post, message, number)))
    last = max(Decimal(message["ts"]) for message in post["messages"])
    for seconds in STEPS:
        clock.now = datetime.fromtimestamp(float(last + seconds), UTC)
        worker.step()
    assert len(judged) <= 1, judged
    return judged[0].verdict if judged and judged[0] is not None else None


def stripped(output: dict[str, Any], *, movement: bool = True, post: bool = True) -> dict[str, Any]:
    varied = copy.deepcopy(output)
    if post:
        varied["post_flags"] = []
    if movement:
        for each in varied["movements"]:
            each["flags"] = []
    return varied


def _text(case: Mapping[str, Any]) -> str:
    return "\n".join(message.get("text", "") for message in case["post"]["messages"]).lower()


def _items(output: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for movement in output["movements"]:
        yield from movement["line_items"]


def more(case: Mapping[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    """Every stated quantity one more, when the post states no such number anywhere."""
    varied = stripped(output, movement=False, post=False)
    for item in _items(varied):
        if item["quantity"] is not None:
            item["quantity"] = int(item["quantity"]) + 1 if float(item["quantity"]).is_integer() else None
    numbers = [item["quantity"] for item in _items(varied) if item["quantity"] is not None]
    if not numbers or any(str(number) in _text(case) for number in numbers):
        return None
    return varied


def other_part(case: Mapping[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
    """Every named item replaced by the exact name of a catalog part the post does not name."""
    names = [part.name for part in CATALOG.parts if part.name.lower() not in _text(case)]
    varied = stripped(output, movement=False, post=False)
    named = [item for item in _items(varied) if item["item_text"] is not None]
    for item in named:
        item["item_text"] = names[0]
    return varied if named else None


def place(side: str, name: str) -> Callable[[Mapping[str, Any], dict[str, Any]], dict[str, Any] | None]:
    """Every movement's place on one side replaced by a name the post does not state."""

    def vary(case: Mapping[str, Any], output: dict[str, Any]) -> dict[str, Any] | None:
        if name.lower() in _text(case) or not output["movements"]:
            return None
        varied = stripped(output, movement=False, post=False)
        for movement in varied["movements"]:
            movement[f"{side}_text"] = name
        return varied

    return vary


MADE_UP = {
    "a quantity one more": more,
    "another part by its exact name": other_part,
    "a vehicle by its exact name": place("destination", "Vehicles/Truck 1"),
    "the poster's own truck": place("destination", "my truck"),
    "a source by its exact name": place("source", "Warehouse/Main Stock"),
}


@pytest.fixture(autouse=True)
def model_reads_every_post(monkeypatch: pytest.MonkeyPatch) -> None:
    """The rules decline every post, so the model's answer is what the worker reads. As built, the model reads only
    what the rules decline, and there its answer is read the same way."""

    def declined(messages: Sequence[MessageInput]) -> Extraction:
        text = "\n".join(message.text for message in messages if not message.deleted)
        return Extraction("rules", (), extract_rules.post_flags(text), declined=True)

    monkeypatch.setattr(extract_rules, "extract", declined)


def test_a_recorded_clear_take_out_is_approved_by_the_policy(migrated: Path, tmp_path: Path) -> None:
    """The harness can approve: the variations below are judged against plans that could apply."""
    [case] = [case for case in CASES if case["id"] == "clear-003"]
    recorded = json.loads(ANSWERS["clear-003"]["output"])
    assert verdict(migrated, tmp_path / "stockthread.db", case, recorded) is Verdict.ALLOWED


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_stripping_the_model_s_flags_never_lets_the_policy_approve(
    migrated: Path, tmp_path: Path, case: dict[str, Any]
) -> None:
    recorded = json.loads(ANSWERS[case["id"]]["output"])
    variants = {
        "no flags": stripped(recorded),
        "no movement flags": stripped(recorded, post=False),
        "no post flags": stripped(recorded, movement=False),
    }
    baseline = verdict(migrated, tmp_path / "recorded.db", case, recorded)
    if baseline is Verdict.ALLOWED:
        return
    for name, output in variants.items():
        assert verdict(migrated, tmp_path / f"{name}.db", case, output) is not Verdict.ALLOWED, (name, baseline)


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_an_answer_that_makes_something_up_is_never_approved_by_the_policy(
    migrated: Path, tmp_path: Path, case: dict[str, Any]
) -> None:
    recorded = json.loads(ANSWERS[case["id"]]["output"])
    for name, vary in MADE_UP.items():
        output = vary(case, recorded)
        if output is None:
            continue
        assert verdict(migrated, tmp_path / f"{name}.db", case, output) is not Verdict.ALLOWED, name


def without_source(output: dict[str, Any]) -> dict[str, Any] | None:
    """Every movement's source left out, the model's own flags kept: the default source must not fill a source the
    post states (ADR 0020)."""
    varied = copy.deepcopy(output)
    sourced = [movement for movement in varied["movements"] if movement["source_text"] is not None]
    for movement in sourced:
        movement["source_text"] = None
    return varied if sourced else None


@pytest.mark.parametrize("case", CASES, ids=lambda case: case["id"])
def test_an_answer_that_leaves_out_a_stated_source_is_never_approved_by_the_policy(
    migrated: Path, tmp_path: Path, case: dict[str, Any]
) -> None:
    output = without_source(json.loads(ANSWERS[case["id"]]["output"]))
    if output is None:
        pytest.skip("the recorded answer names no source")
    assert verdict(migrated, tmp_path / "stockthread.db", case, output) is not Verdict.ALLOWED
