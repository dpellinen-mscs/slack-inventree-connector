# SPDX-License-Identifier: MIT
"""How the worker reads a post's movements: each movement as a reply shows it (`MovementView`), with its matched
parts and places and its review reasons, before any answer and with the effects of the answers given."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import replace
from functools import cache, partial

from stockthread.core.answers import Effects
from stockthread.core.matching import Catalog, locations_named, match_part
from stockthread.core.model import (
    CandidateMovement,
    Extraction,
    LineItem,
    Match,
    MatchStatus,
    MessageInput,
    ReasonCode,
    ReasonSource,
    ReviewReason,
)
from stockthread.core.questions import reason_line
from stockthread.core.validate import DETAIL_OTHER_AUTHOR, in_stocking_unit, photo_not_read, validate
from stockthread.places import by_default, default_source, is_default, located, place_left_out, rival_named
from stockthread.slack.replies import MovementView


def reading_views(
    extraction: Extraction,
    messages: Sequence[MessageInput],
    poster: str,
    catalog: Catalog,
    late: bool,
    others: bool,
    *,
    max_quantity: int,
) -> list[MovementView]:
    """The movements of a reading as a reply shows them: each line's part, the source and destination, and the review
    reasons, before any answer."""
    views = []
    named = cache(partial(locations_named, _post_text(messages), catalog, poster))
    for movement in extraction.movements:
        items = [_part(item.item_text, catalog, poster) for item in movement.line_items]
        stocking = _stocking(items, catalog)
        reasons = _reasons(movement, extraction, messages, late, others, stocking, max_quantity=max_quantity)
        reasons = place_left_out(movement, extraction.movements, named, catalog, poster, reasons)
        reasons = rival_named(movement, (), _post_text(messages), catalog, poster, reasons)
        source = located(movement.source_text, catalog, poster) or default_source(movement, catalog, reasons)
        reasons = by_default(reasons, source)
        views.append(
            MovementView(
                movement=movement,
                items=items,
                source=source,
                destination=located(movement.destination_text, catalog, poster),
                reasons=reasons,
                units=_own_units(movement, stocking),
            )
        )
    return views


def _reasons(
    movement: CandidateMovement,
    extraction: Extraction,
    messages: Sequence[MessageInput],
    late: bool,
    others: bool,
    stocking: Sequence[str | None] = (),
    *,
    max_quantity: int,
) -> tuple[ReviewReason, ...]:
    """The movement's reasons. `others`: a message read is not the poster's. `stocking`: the units of each
    line's part, for a part that can authorize a write (`_stocking`). `max_quantity`: `policy.max_quantity`."""
    text = _post_text(messages)
    lateness = (ReviewReason(ReasonCode.LATE_INGEST, ReasonSource.RULE),) if late else ()
    # No extractor sees a photo's pixels (design section 13): the photo may show other items, another
    # quantity or serials, so a person checks the reading against it.
    unread = (ReviewReason(ReasonCode.PHOTO_NOT_READ, ReasonSource.RULE),) if photo_not_read(messages) else ()
    # Another person's reply joined to the post: "my truck" there is read as the poster's (T6).
    mixed = (ReviewReason(ReasonCode.ACTOR_NOT_POSTER, ReasonSource.RULE, DETAIL_OTHER_AUTHOR),) if others else ()
    return (
        lateness
        + unread
        + mixed
        + validate(
            movement,
            text,
            max_quantity=max_quantity,
            post_flags=extraction.post_flags,
            stocking=stocking,
            kinds={read.movement_type for read in extraction.movements},
        )
    )


def with_answers(
    views: Sequence[MovementView],
    effects: Effects,
    extraction: Extraction,
    messages: Sequence[MessageInput],
    late: bool,
    catalog: Catalog,
    poster: str,
    others: bool,
    *,
    max_quantity: int,
) -> list[MovementView]:
    """The views with the answers' effects: a picked type is validated again, a picked or confirmed record
    replaces its match, and a cleared reason is left out. A quantity or serials typed in the thread replace the
    line's own; validation then runs again on that movement, with the answer messages' text in the post. So it
    does when a picked or confirmed part changes the units a line may be stocked in (`_stocking`)."""
    found = []
    named = cache(partial(locations_named, _post_text(messages), catalog, poster))
    for n, view in enumerate(views):
        movement, reasons = view.movement, view.reasons
        items = [effects.parts.get((n, line), match) for line, match in enumerate(view.items)]
        stocking = _stocking(items, catalog)
        if n in effects.types:
            movement = replace(movement, movement_type=effects.types[n])
        if n in effects.retyped:
            movement = replace(
                movement,
                line_items=tuple(_typed_line(effects, n, line, item) for line, item in enumerate(movement.line_items)),
            )
        if n in effects.types or n in effects.retyped or stocking != _stocking(view.items, catalog):
            reasons = _reasons(movement, extraction, messages, late, others, stocking, max_quantity=max_quantity)
            reasons = place_left_out(movement, extraction.movements, named, catalog, poster, reasons)
            # Unreachable with a rival pending today: its confirm is a reading question, asked before any
            # whose answer recomputes, and a reading that changes starts the answers over. Kept so that a
            # recompute reads the post as the first pass did.
            picked = (effects.places.get((n, "source")), effects.places.get((n, "destination")))
            reasons = rival_named(movement, picked, _post_text(messages), catalog, poster, reasons)
        source = effects.places.get((n, "source"))
        if source is None:
            # A picked type takes its own default source, or none.
            source = (
                default_source(movement, catalog, reasons)
                if is_default(view.source) or (n in effects.types and view.source is None)
                else view.source
            )
        reasons = by_default(reasons, source)
        found.append(
            MovementView(
                movement=movement,
                items=items,
                source=source,
                destination=effects.places.get((n, "destination"), view.destination),
                reasons=tuple(
                    reason
                    for reason in reasons
                    if (n, reason_line(reason, movement), reason.code, reason.detail) not in effects.cleared
                    and (n, reason_line(reason, movement), reason.code) not in effects.settled
                ),
                named={key[1]: text for key, text in effects.named.items() if key[0] == n},
                units=_own_units(movement, stocking),
            )
        )
    return found


def _typed_line(effects: Effects, movement: int, line: int, item: LineItem) -> LineItem:
    """A line item with the quantity and serials typed for it in the thread. A quantity typed for a pack-unit
    question counts pieces: the line's unit is the piece and its pack size is dropped. A line the post named no item
    for takes the typed name as its item text, so "items missing" moves on to the next line without one."""
    key = (movement, line)
    if key in effects.named and not (item.item_text or "").strip():
        item = replace(item, item_text=effects.named[key])
    if key in effects.quantities:
        item = replace(item, quantity=effects.quantities[key])
    if key in effects.pieces:
        item = replace(item, unit=None, pack_size=None)
    if key in effects.serials:
        item = replace(item, serials=effects.serials[key])
    return item


def _part(text: str | None, catalog: Catalog, poster: str) -> Match:
    return match_part(text, catalog, poster) if text else Match(MatchStatus.UNMATCHED)


def _stocking(items: Sequence[Match], catalog: Catalog) -> tuple[str | None, ...]:
    """The units of each line's part (`Part.units`), for a part matched so that it can authorize a write, else None:
    what validate may spare a length in the part's own unit for. A guessed part's units are never trusted."""
    units = {part.pk: part.units for part in catalog.parts}
    return tuple(
        units.get(match.record) if match.can_authorize_a_write and match.record is not None else None for match in items
    )


def _own_units(movement: CandidateMovement, stocking: Sequence[str | None]) -> tuple[str | None, ...]:
    """`MovementView.units`: the part's units for each line in its part's own length unit, else None; () for none."""
    units = tuple(
        found if in_stocking_unit(item.unit, found) else None
        for item, found in zip(movement.line_items, stocking, strict=True)
    )
    return units if any(unit is not None for unit in units) else ()


def _post_text(messages: Sequence[MessageInput]) -> str:
    return "\n".join(message.text for message in messages if not message.deleted)
