# SPDX-License-Identifier: MIT
"""Helpers of the integration tests that talk to the disposable InvenTree (`tests/integration/start-inventree.sh`):
the settings for it, and the reads and movements several test modules share."""

from __future__ import annotations

import os
from pathlib import Path
from typing import TYPE_CHECKING

import anyio
from views import picked, view

from stockthread.config import Settings, parse_settings
from stockthread.core.model import Match, MatchRule, MatchStatus, MovementType
from stockthread.inventree import InvenTreeReader, MarkedEntry
from stockthread.slack.replies import MovementView

if TYPE_CHECKING:
    from conftest import Scratch


def settings() -> Settings:
    token = Path(os.environ["STOCKTHREAD_IT_TOKEN_FILE"]).read_text(encoding="utf-8").strip()
    data = {
        "slack": {"workspace_id": "T0ACME001", "app_id": "A0ACME001", "channels": ["C0ACME001"]},
        "inventree": {"url": os.environ["STOCKTHREAD_IT_URL"]},
    }
    env = {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": token}
    return parse_settings(data, env)


def histories(pks: set[int]) -> dict[int, list[int]]:
    async def search() -> dict[int, list[int]]:
        found = settings()
        async with InvenTreeReader(found.inventree, found.secrets.inventree_token) as reader:
            return {pk: await reader.item_entries(pk) for pk in sorted(pks)}

    return anyio.run(search)


def entries(marker_id: str) -> list[MarkedEntry]:
    async def search() -> list[MarkedEntry]:
        found = settings()
        async with InvenTreeReader(found.inventree, found.secrets.inventree_token) as reader:
            return await reader.marker_entries(marker_id)

    return anyio.run(search)


def planned(scratch: Scratch, movement_type: MovementType, pk: int, quantity: str) -> MovementView:
    place = Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.parent)
    other = Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.other)
    acts_at_source = movement_type in (MovementType.TAKE_TO_VEHICLE, MovementType.CONSUME)
    return view(
        movement_type,
        lines=(("Disc 40", quantity),),
        stock=(picked((pk, quantity)),),
        items=(Match(MatchStatus.MATCHED, MatchRule.EXACT, scratch.part),),
        source=place if acts_at_source else None,
        destination=None if movement_type is MovementType.CONSUME else (other if acts_at_source else place),
    )
