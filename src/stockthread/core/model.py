"""The domain model of the pure core: posts, candidates, match results and review reasons.

Values are frozen dataclasses that check their own invariants, so an
impossible value (a "matched" result with two parts, a negative quantity)
cannot be built. Adapters validate outside input before they build these.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from decimal import Decimal
from enum import StrEnum
from typing import Literal


class MovementType(StrEnum):
    TAKE_TO_VEHICLE = "take_to_vehicle"
    TAKE_TO_SITE = "take_to_site"
    RETURN = "return"
    TRANSFER = "transfer"
    RECEIVE = "receive"
    CONSUME = "consume"
    COUNT = "count"
    UNCLEAR = "unclear"


class Operation(StrEnum):
    """The InvenTree stock operation a movement becomes (design section 5)."""

    TRANSFER = "transfer"
    REMOVE = "remove"
    ADD = "add"
    COUNT = "count"


def operation_for(movement_type: MovementType, *, consume_as_transfer: bool = False) -> Operation | None:
    """The operation for a movement type. None for `unclear`, which never becomes a request."""
    match movement_type:
        case MovementType.TAKE_TO_VEHICLE | MovementType.TAKE_TO_SITE | MovementType.RETURN | MovementType.TRANSFER:
            return Operation.TRANSFER
        case MovementType.CONSUME:
            return Operation.TRANSFER if consume_as_transfer else Operation.REMOVE
        case MovementType.RECEIVE:
            return Operation.ADD
        case MovementType.COUNT:
            return Operation.COUNT
        case MovementType.UNCLEAR:
            return None


class Flag(StrEnum):
    """What an extractor may say about a movement. The eval corpus uses the same vocabulary."""

    IMPLIED_SOURCE = "implied_source"
    IMPLIED_DESTINATION = "implied_destination"
    QUANTITY_MISSING = "quantity_missing"
    QUANTITY_VAGUE = "quantity_vague"
    PACK_UNIT_AMBIGUOUS = "pack_unit_ambiguous"
    ITEMS_MISSING = "items_missing"
    PHOTO_ONLY = "photo_only"
    QUANTITY_FROM_PHOTO = "quantity_from_photo"
    SERIAL_UNCONFIRMED = "serial_unconfirmed"
    PHOTO_TEXT_MISMATCH = "photo_text_mismatch"
    CORRECTED_IN_THREAD = "corrected_in_thread"
    ACTOR_NOT_POSTER = "actor_not_poster"


class PostFlag(StrEnum):
    """What an extractor may say about a whole post."""

    SUSPICIOUS_INSTRUCTIONS = "suspicious_instructions"
    CONTAINS_SENSITIVE = "contains_sensitive"


class ReasonSource(StrEnum):
    RULE = "rule"
    """Derived deterministically from the text, the candidate, the catalog or the policy."""
    MODEL = "model"
    """Raised by an extractor. A model can add a reason, never remove one."""


class ReasonCode(StrEnum):
    """Why a movement needs a human. Every open reason blocks a write."""

    # Raised by an extractor, or derived by rule from the text.
    IMPLIED_SOURCE = "implied_source"
    IMPLIED_DESTINATION = "implied_destination"
    QUANTITY_MISSING = "quantity_missing"
    QUANTITY_VAGUE = "quantity_vague"
    PACK_UNIT_AMBIGUOUS = "pack_unit_ambiguous"
    ITEMS_MISSING = "items_missing"
    PHOTO_ONLY = "photo_only"
    QUANTITY_FROM_PHOTO = "quantity_from_photo"
    SERIAL_UNCONFIRMED = "serial_unconfirmed"
    PHOTO_TEXT_MISMATCH = "photo_text_mismatch"
    CORRECTED_IN_THREAD = "corrected_in_thread"
    ACTOR_NOT_POSTER = "actor_not_poster"
    SUSPICIOUS_INSTRUCTIONS = "suspicious_instructions"
    # Derived by rule only.
    MOVEMENT_UNCLEAR = "movement_unclear"
    CONTRADICTION = "contradiction"
    ITEM_NOT_IN_TEXT = "item_not_in_text"
    LOCATION_NOT_IN_TEXT = "location_not_in_text"
    QUANTITY_NOT_IN_TEXT = "quantity_not_in_text"
    QUANTITY_ABOVE_LIMIT = "quantity_above_limit"
    QUANTITY_ABOVE_STOCK = "quantity_above_stock"
    PART_UNMATCHED = "part_unmatched"
    PART_AMBIGUOUS = "part_ambiguous"
    LOCATION_UNMATCHED = "location_unmatched"
    LOCATION_AMBIGUOUS = "location_ambiguous"
    FUZZY_MATCH = "fuzzy_match"
    VEHICLE_NOT_MAPPED = "vehicle_not_mapped"
    STOCK_CHOICE = "stock_choice"
    NO_TARGET_STOCK_ITEM = "no_target_stock_item"
    EXTRACTORS_DISAGREE = "extractors_disagree"
    LATE_INGEST = "late_ingest"
    PRECONDITION_FAILED = "precondition_failed"


@dataclass(frozen=True)
class ReviewReason:
    code: ReasonCode
    source: ReasonSource
    detail: str = ""
    """A short explanation for the reviewer. Never the message text itself."""


def reason_for_flag(flag: Flag | PostFlag) -> ReviewReason:
    """Every extractor flag is a review reason. CONTAINS_SENSITIVE is about logging, not about the write."""
    if flag is PostFlag.CONTAINS_SENSITIVE:
        raise ValueError("contains_sensitive is not a review reason")
    return ReviewReason(ReasonCode(flag.value), ReasonSource.MODEL)


@dataclass(frozen=True, order=True)
class PostKey:
    """The idempotency key of a post: fixed when the post is created, never recomputed."""

    workspace_id: str
    channel_id: str
    ts: str
    """The Slack timestamp of the first message, kept as the exact string Slack sent."""

    def __post_init__(self) -> None:
        if not (self.workspace_id and self.channel_id and self.ts):
            raise ValueError("a post key needs a workspace, a channel and a timestamp")
        if any("/" in part for part in (self.workspace_id, self.channel_id, self.ts)):
            raise ValueError("a post key part cannot contain '/'")

    def __str__(self) -> str:
        return f"{self.workspace_id}/{self.channel_id}/{self.ts}"


@dataclass(frozen=True, order=True)
class RequestKey:
    """The idempotency key of one InvenTree write: a post, a plan revision and a sequence number."""

    post: PostKey
    revision: int
    sequence: int

    def __post_init__(self) -> None:
        if self.revision < 1 or self.sequence < 1:
            raise ValueError("revision and sequence start at 1")

    def __str__(self) -> str:
        return f"{self.post}/r{self.revision}/{self.sequence}"


@dataclass(frozen=True)
class MessageInput:
    """What a plan revision was built from, for one Slack message."""

    ts: str
    text: str
    edited_ts: str | None = None
    file_ids: tuple[str, ...] = ()
    deleted: bool = False


def input_hash(messages: Iterable[MessageInput]) -> str:
    """A hash of the messages a plan was built from. Any edit, deletion or new message changes it.

    The order of the messages does not matter, because Slack does not guarantee event order.
    """
    canonical = sorted(
        (message.ts, message.text, message.edited_ts, sorted(message.file_ids), message.deleted) for message in messages
    )
    payload = json.dumps(canonical, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class LineItem:
    item_text: str | None
    quantity: Decimal | None
    unit: str | None = None
    pack_size: Decimal | None = None
    serials: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if self.quantity is not None and (not self.quantity.is_finite() or self.quantity < 0):
            raise ValueError("a quantity is a finite number, zero or more")
        if self.pack_size is not None and (not self.pack_size.is_finite() or self.pack_size <= 0):
            raise ValueError("a pack size is a finite number above zero")
        if len(set(self.serials)) != len(self.serials):
            raise ValueError("a serial number is listed twice")


@dataclass(frozen=True)
class CandidateMovement:
    """One movement as an extractor proposed it. Text only: nothing is matched yet."""

    movement_type: MovementType
    line_items: tuple[LineItem, ...]
    source_text: str | None
    destination_text: str | None
    flags: frozenset[Flag] = field(default_factory=frozenset)


@dataclass(frozen=True)
class Extraction:
    extractor: str
    """Name and version of the extractor, and of the prompt for an LLM extractor."""
    movements: tuple[CandidateMovement, ...]
    post_flags: frozenset[PostFlag] = field(default_factory=frozenset)
    declined: bool = False
    """The extractor could not read the post. No movements then does not mean "not a movement"."""

    def __post_init__(self) -> None:
        if self.declined and self.movements:
            raise ValueError("a declined extraction has no movements")


class MatchStatus(StrEnum):
    MATCHED = "matched"
    AMBIGUOUS = "ambiguous"
    UNMATCHED = "unmatched"


class MatchRule(StrEnum):
    """The rule that produced a match (design section 10). Only the first three can authorize a write."""

    EXACT = "exact"
    NORMALIZED = "normalized"
    ALIAS = "alias"
    FUZZY = "fuzzy"


@dataclass(frozen=True)
class Match:
    """A part or a location match. `record` and `candidates` are InvenTree primary keys."""

    status: MatchStatus
    rule: MatchRule | None = None
    record: int | None = None
    candidates: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        if self.status is MatchStatus.MATCHED:
            if self.record is None or self.rule is None or self.candidates:
                raise ValueError("a match names one record and its rule, and no candidates")
        elif self.record is not None:
            raise ValueError(f"an {self.status} result names no record")
        if self.status is MatchStatus.AMBIGUOUS and len(set(self.candidates)) < 2:
            raise ValueError("an ambiguous result has at least two different candidates")
        if self.status is MatchStatus.UNMATCHED and (self.candidates or self.rule is not None):
            raise ValueError("an unmatched result has no candidates and no rule")
        if len(set(self.candidates)) != len(self.candidates):
            raise ValueError("a candidate is listed twice")

    @property
    def can_authorize_a_write(self) -> bool:
        """True only for one record found by an exact, normalized or alias rule."""
        return self.status is MatchStatus.MATCHED and self.rule is not MatchRule.FUZZY

    def review_reason(self, *, kind: Literal["part", "location"]) -> ReviewReason | None:
        """The review reason this result raises, or None."""
        if self.can_authorize_a_write:
            return None
        if self.status is MatchStatus.MATCHED:
            return ReviewReason(ReasonCode.FUZZY_MATCH, ReasonSource.RULE, f"{kind} found by a fuzzy rule")
        codes = {
            ("part", MatchStatus.AMBIGUOUS): ReasonCode.PART_AMBIGUOUS,
            ("part", MatchStatus.UNMATCHED): ReasonCode.PART_UNMATCHED,
            ("location", MatchStatus.AMBIGUOUS): ReasonCode.LOCATION_AMBIGUOUS,
            ("location", MatchStatus.UNMATCHED): ReasonCode.LOCATION_UNMATCHED,
        }
        return ReviewReason(codes[(kind, self.status)], ReasonSource.RULE)
