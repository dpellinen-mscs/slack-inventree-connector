# SPDX-License-Identifier: MIT
"""Grouping: which post a Slack message belongs to (design section 4, "Grouping").

A photo with its caption is already one message. Sibling messages from the same user within a short
window, and replies in a thread, join one post. A message belongs to exactly one post, and a post keeps
the timestamp it was created with, because Slack does not guarantee the order of events: a reply can
arrive before its thread root, and a later sibling before an earlier one.

Pure: the store supplies what it knows about nearby messages and posts, and records the answer.
"""

from __future__ import annotations

from collections.abc import Collection, Sequence
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class Arrival:
    """A message to place: its timestamp, author, and thread root (None outside a thread)."""

    ts: str
    user: str
    thread_ts: str | None = None


@dataclass(frozen=True)
class Placed:
    """A message already in a post, and whether that post still takes siblings."""

    ts: str
    user: str
    thread_ts: str | None
    post_ts: str
    open_to_siblings: bool

    @property
    def is_reply(self) -> bool:
        return self.thread_ts is not None and self.thread_ts != self.ts


def seconds(ts: str) -> Decimal:
    """A Slack timestamp as a number. Kept exact: a float cannot hold every microsecond of it."""
    try:
        value = Decimal(ts)
    except InvalidOperation:
        raise ValueError(f"not a Slack timestamp: {ts!r}") from None
    if not value.is_finite() or value < 0:
        raise ValueError(f"not a Slack timestamp: {ts!r}")
    return value


def choose_post(arrival: Arrival, placed: Sequence[Placed], post_keys: Collection[str], window: int) -> str:
    """The key timestamp of the post the message joins; its own timestamp when it starts a new post.

    `placed` holds the messages of the same channel that could matter (the same timestamp, the thread
    root, and the same user's messages near it); `post_keys` the key timestamps of posts in the channel
    that could matter (this timestamp and the thread root).
    """
    for message in placed:
        if message.ts == arrival.ts:
            return message.post_ts  # a message is placed once; a redelivery gets the same answer
    root = arrival.thread_ts
    if root is not None and root != arrival.ts:
        for message in placed:
            if message.ts == root:
                return message.post_ts  # the root is in a post, perhaps as a sibling of an earlier one
        return root  # the root has not arrived: the post is keyed on it, and it joins later
    if arrival.ts in post_keys:
        return arrival.ts  # a reply arrived first and created this post; its root is the first message
    at = seconds(arrival.ts)
    near = [
        message
        for message in placed
        if message.user == arrival.user
        and not message.is_reply
        and message.open_to_siblings
        and abs(seconds(message.ts) - at) <= window
    ]
    if not near:
        return arrival.ts
    nearest = min(near, key=lambda message: (abs(seconds(message.ts) - at), seconds(message.ts)))
    return nearest.post_ts
