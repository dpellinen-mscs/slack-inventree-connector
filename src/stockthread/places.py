# SPDX-License-Identifier: MIT
"""The places of a movement as the worker reads them: its source and destination matched in the catalog, the movement
type's default source (`policy.default_source`, ADR 0020) for a source the text does not state, and the checks that
keep a default or a reading from moving stock from a place the post did not mean (threat model T6, T19)."""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import replace

from stockthread.core.matching import Catalog, locations_named, match_location
from stockthread.core.model import (
    CandidateMovement,
    Match,
    MatchRule,
    MatchStatus,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.core.validate import DETAIL_MIXED, DETAIL_PLACE_NAMED, DETAIL_SOURCE_LEFT_OUT, clauses_drawn_on


def located(text: str | None, catalog: Catalog, poster: str) -> Match | None:
    return match_location(text, catalog, poster) if text else None


def is_default(match: Match | None) -> bool:
    return match is not None and match.rule is MatchRule.DEFAULT


def by_default(reasons: Sequence[ReviewReason], source: Match | None) -> tuple[ReviewReason, ...]:
    """The reasons without the implied source, by rule or by the model, when the source is the movement type's
    default: the admin's policy says where an unstated source is (design section 6, "Implied source")."""
    if not is_default(source):
        return tuple(reasons)
    return tuple(reason for reason in reasons if reason.code is not ReasonCode.IMPLIED_SOURCE)


_NAMED = (DETAIL_SOURCE_LEFT_OUT, DETAIL_PLACE_NAMED)
"""The implied-source details of a text that names a source: no default settles them."""


_SURE = (MatchRule.EXACT, MatchRule.NORMALIZED, MatchRule.ALIAS)
"""The rules whose single match accounts for a named place (`place_left_out`); the whole-word and fuzzy rules guess."""


def place_left_out(
    movement: CandidateMovement,
    readings: Sequence[CandidateMovement],
    named: Callable[[], frozenset[int]],
    catalog: Catalog,
    poster: str,
    reasons: Sequence[ReviewReason],
) -> tuple[ReviewReason, ...]:
    """The reasons, with the rule's implied source for a text with no "from" word marked `DETAIL_PLACE_NAMED` when
    the text names (`named`: the post's `locations_named`, read once per post) a stock location that is neither a
    destination of the post's movements (`readings`; matched by name or alias: "the shelf" may be any shelf) nor the
    movement type's default source: "pulled 2 ONTs shelf B to van 3" states shelf B as the source (T19), so the
    default must not settle it."""
    unstated = ReviewReason(ReasonCode.IMPLIED_SOURCE, ReasonSource.RULE)
    default = catalog.default_sources.get(movement.movement_type)
    if movement.source_text is not None or default is None or unstated not in reasons:
        return tuple(reasons)  # with no default the source is asked for anyway
    sides = {default}
    for read in readings:  # a destination of the post's movements, found for sure, is accounted for
        destination = located(read.destination_text, catalog, poster)
        if destination is not None and destination.rule in _SURE and destination.record is not None:
            sides.add(destination.record)
    if not named() - sides:
        return tuple(reasons)
    marked = replace(unstated, detail=DETAIL_PLACE_NAMED)
    return tuple(marked if reason == unstated else reason for reason in reasons)


def rival_named(
    movement: CandidateMovement,
    picked: Sequence[Match | None],
    text: str,
    catalog: Catalog,
    poster: str,
    reasons: Sequence[ReviewReason],
) -> tuple[ReviewReason, ...]:
    """The reasons, with `validate.DETAIL_MIXED` added when a clause the movement draws on from another
    (`validate.clauses_drawn_on`) names a stock location that is not one of its places: "took 2 ONTs to van 3,
    Riverside Apartments got 4 drops" read as 6 items to van 3. The rules know vehicles, jobs and sites by their
    words; a place named by neither ("Riverside Apartments") only the catalog knows (T6). Its places are its source
    and destination as read or `picked` by a person (source first), and its type's default source when it states
    none and none was picked. Unlike in `place_left_out`, a
    destination of the post's other movements is no place of its own: a model could add a movement to Riverside
    Apartments to hide the rival."""
    mixed = ReviewReason(ReasonCode.MOVEMENT_UNCLEAR, ReasonSource.RULE, DETAIL_MIXED)
    if mixed in reasons:
        return tuple(reasons)
    drawn_on = clauses_drawn_on(movement, text)
    if not drawn_on:
        return tuple(reasons)
    places = [
        *picked,
        located(movement.source_text, catalog, poster),
        located(movement.destination_text, catalog, poster),
    ]
    mine = {place.record for place in places if place is not None and place.record is not None}
    default = catalog.default_sources.get(movement.movement_type)
    if default is not None and movement.source_text is None and (not picked or picked[0] is None):
        mine.add(default)  # only a movement that takes its source from the default: "from shelf B" does not
    # One search over all of them: each reads the whole catalog. A line break ends a name as a comma does.
    if not locations_named("\n".join(drawn_on), catalog, poster) - mine:
        return tuple(reasons)
    return (*reasons, mixed)


def default_source(movement: CandidateMovement, catalog: Catalog, reasons: Sequence[ReviewReason]) -> Match | None:
    """The movement type's `policy.default_source`, for a movement that states no source; else None. A text that
    names a source the reading left out ("from shelf B") gets none: the default is for an unstated source, and a
    model must not move stock from it by leaving the stated one out (`validate.DETAIL_SOURCE_LEFT_OUT`), nor from it
    when the text names another stock location (`place_left_out`)."""
    if movement.source_text is not None or movement.movement_type not in catalog.default_sources:
        return None
    if any(reason.code is ReasonCode.IMPLIED_SOURCE and reason.detail in _NAMED for reason in reasons):
        return None
    return Match(MatchStatus.MATCHED, MatchRule.DEFAULT, catalog.default_sources[movement.movement_type])
