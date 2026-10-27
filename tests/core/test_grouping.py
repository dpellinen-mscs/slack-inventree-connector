# SPDX-License-Identifier: MIT
"""Grouping: which post a message joins, whatever order Slack delivers them in (design section 4)."""

from __future__ import annotations

import itertools

import pytest

from stockthread.core.grouping import Arrival, Placed, choose_post, seconds

WINDOW = 5


def placed(ts: str, post_ts: str | None = None, *, user: str = "U1", thread_ts: str | None = None) -> Placed:
    return Placed(ts, user, thread_ts, post_ts or ts, open_to_siblings=True)


def test_a_lone_message_starts_its_own_post() -> None:
    assert choose_post(Arrival("100.000001", "U1"), [], [], WINDOW) == "100.000001"


def test_a_sibling_within_the_window_joins_the_nearest_post() -> None:
    known = [placed("100.0"), placed("110.0")]
    assert choose_post(Arrival("104.0", "U1"), known, ["100.0", "110.0"], WINDOW) == "100.0"
    assert choose_post(Arrival("105.0", "U1"), known, ["100.0", "110.0"], WINDOW) == "100.0"  # a tie goes earlier
    assert choose_post(Arrival("106.0", "U1"), known, ["100.0", "110.0"], WINDOW) == "110.0"


def test_an_earlier_sibling_that_arrives_late_joins_without_rekeying() -> None:
    assert choose_post(Arrival("98.0", "U1"), [placed("100.0")], ["100.0"], WINDOW) == "100.0"


@pytest.mark.parametrize(
    "other",
    [
        placed("100.0", user="U2"),  # someone else
        placed("94.999999"),  # outside the window, by a microsecond
        placed("100.0", thread_ts="90.0"),  # a reply in a thread is not a sibling of a channel message
        Placed("100.0", "U1", None, "100.0", open_to_siblings=False),  # the dry-run reply closed it
    ],
)
def test_what_is_not_a_sibling(other: Placed) -> None:
    arrival = Arrival("100.000001", "U1")
    assert choose_post(arrival, [other], [other.post_ts], WINDOW) == arrival.ts


def test_a_reply_joins_the_post_of_its_root_even_when_the_root_was_a_sibling() -> None:
    known = [placed("100.0"), placed("103.0", "100.0")]
    assert choose_post(Arrival("200.0", "U2", thread_ts="103.0"), known, ["100.0"], WINDOW) == "100.0"


def test_a_reply_joins_a_closed_post() -> None:
    known = [Placed("100.0", "U1", None, "100.0", open_to_siblings=False)]
    assert choose_post(Arrival("300.0", "U1", thread_ts="100.0"), known, ["100.0"], WINDOW) == "100.0"


def test_a_reply_before_its_root_keys_the_post_on_the_root() -> None:
    assert choose_post(Arrival("200.0", "U2", thread_ts="100.0"), [], [], WINDOW) == "100.0"
    # The root then arrives, and is the first message of that post, not a sibling of anything nearby.
    near = [placed("98.0")]
    assert choose_post(Arrival("100.0", "U1", thread_ts="100.0"), near, ["100.0", "98.0"], WINDOW) == "100.0"
    assert choose_post(Arrival("100.0", "U1"), near, ["100.0", "98.0"], WINDOW) == "100.0"


def test_a_redelivered_message_gets_the_same_post() -> None:
    known = [placed("103.0", "100.0")]
    assert choose_post(Arrival("103.0", "U1"), known, ["100.0"], WINDOW) == "100.0"


def test_a_reply_stays_with_its_root_in_every_arrival_order() -> None:
    """A root, its reply and a sibling, in all six orders. Whether the sibling joins can depend on the order."""
    messages = [Arrival("100.0", "U1", "100.0"), Arrival("102.0", "U1"), Arrival("150.0", "U2", "100.0")]
    for order in itertools.permutations(messages):
        known: list[Placed] = []
        for arrival in order:
            key = choose_post(arrival, known, {m.post_ts for m in known}, WINDOW)
            known.append(Placed(arrival.ts, arrival.user, arrival.thread_ts, key, open_to_siblings=True))
        posts = {m.ts: m.post_ts for m in known}
        assert posts["100.0"] == posts["150.0"], order  # the reply is always with its root
        assert posts["102.0"] in ("100.0", "102.0"), order


@pytest.mark.parametrize("bad", ["", "abc", "NaN", "-1.0", "Infinity"])
def test_a_timestamp_must_be_a_number(bad: str) -> None:
    with pytest.raises(ValueError, match="not a Slack timestamp"):
        seconds(bad)


def test_timestamps_are_exact() -> None:
    assert seconds("1790000000.000101") - seconds("1790000000.000100") == seconds("0.000001")


def test_siblings_chain_so_the_order_can_decide() -> None:
    """Documented: the window is measured from the nearest message, so 0, 4, 8 is one post but 0, 8, 4 is two."""
    known: list[Placed] = []
    for ts in ("0.0", "8.0", "4.0"):
        post = choose_post(Arrival(ts, "U1"), known, {m.post_ts for m in known}, WINDOW)
        known.append(Placed(ts, "U1", None, post, open_to_siblings=True))
    assert [m.post_ts for m in known] == ["0.0", "8.0", "0.0"]
