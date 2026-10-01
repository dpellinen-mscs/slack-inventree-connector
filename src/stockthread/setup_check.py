"""The setup check (`stockthread check-setup`): what an admin should fix before the first dry run (design section 5,
"Locations" and "Least privilege").

It reads only: the InvenTree version, the roles of the token's user, the stock locations, and which Slack users
have posted in the watched channels (from the local database, ids only). Every finding is a fixed sentence about
settings, roles or paths; nothing of a message is read or printed.

Pure: facts in, findings out. The command (`__main__.check_setup`) gathers the facts.
"""

from __future__ import annotations

import enum
from collections.abc import Collection, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from stockthread.core.matching import Catalog

MINIMUM_ROLES: Mapping[str, frozenset[str]] = {
    "part": frozenset({"view"}),
    "stock_location": frozenset({"view"}),
    "stock": frozenset({"view", "add"}),
}
"""The documented minimum for the connector's user (design section 5, `test_char_restricted_user_can_adjust`)."""
IMPLIED = {"stock": frozenset({"change"})}
"""Granted with "add" on Stock Item by the server itself: never reported as more than needed."""
_NEEDED_FOR = {
    ("part", "view"): "dry runs (the catalog cannot be read)",
    ("stock_location", "view"): "dry runs (the catalog cannot be read)",
    ("stock", "view"): "source selection (stock items cannot be read)",
    ("stock", "add"): "writes (every stock adjustment is refused)",
}


class Level(enum.StrEnum):
    OK = "ok"
    WARN = "warn"
    ERROR = "error"


@dataclass(frozen=True)
class Finding:
    level: Level
    text: str


def check_roles(body: Mapping[str, Any]) -> list[Finding]:
    """What the token's user (`GET /api/user/roles/`) lacks, and what it has beyond the minimum."""
    roles = body.get("roles")
    if not isinstance(roles, Mapping):
        return [Finding(Level.ERROR, "roles: the server did not say which roles the token's user has")]
    granted = {
        name: frozenset(p for p in perms if isinstance(p, str)) if isinstance(perms, list) else frozenset()
        for name, perms in roles.items()
    }
    found = []
    for name, needed in MINIMUM_ROLES.items():
        for permission in sorted(needed - granted.get(name, frozenset())):
            found.append(
                Finding(Level.ERROR, f"roles: {name} lacks {permission}, needed for {_NEEDED_FOR[(name, permission)]}")
            )
    if body.get("is_superuser") is True:  # every permission, whatever the roles say: listing them adds nothing
        found.append(Finding(Level.WARN, "roles: the token's user is a superuser; use a dedicated user"))
        return found
    if body.get("is_staff") is True:
        found.append(Finding(Level.WARN, "roles: the token's user is staff; use a dedicated user"))
    for name in sorted(granted):
        extra = granted[name] - MINIMUM_ROLES.get(name, frozenset()) - IMPLIED.get(name, frozenset())
        if extra:
            found.append(Finding(Level.WARN, f"roles: {name} has {', '.join(sorted(extra))}, more than needed"))
    if not found:
        found.append(Finding(Level.OK, "roles: the token's user has the minimum roles and no more"))
    return found


def check_vehicles(catalog: Catalog, vehicles: Mapping[str, str]) -> list[Finding]:
    """Each `[vehicles]` path: a stock location that can hold stock, or why not (as `assign_vehicles` decides)."""
    by_path: dict[str, list[bool]] = {}
    for location in catalog.locations:
        by_path.setdefault(location.path, []).append(location.structural)
    found = []
    for user, path in sorted(vehicles.items()):
        at = by_path.get(path, [])
        holding = at.count(False)
        if holding == 1:
            continue
        if holding > 1:
            why = "names more than one stock location"
        elif at:
            why = "is a structural location, which cannot hold stock"
        else:
            why = "is not the whole path of any stock location (paths match letter for letter)"
        found.append(Finding(Level.ERROR, f'vehicles: {user}: "{path}" {why}'))
    if vehicles and not found:
        found.append(Finding(Level.OK, f"vehicles: every one of the {len(vehicles)} paths is a stock location"))
    return found


def check_posters(posters: Sequence[str] | None, vehicles: Collection[str]) -> list[Finding]:
    """Slack users who have posted in the watched channels with no vehicle set: their "my truck" is not matched."""
    if posters is None:
        return [Finding(Level.OK, "posters: no database yet, so nobody has posted; run this again after a few posts")]
    without = sorted(set(posters) - set(vehicles))
    if not without:
        return [Finding(Level.OK, f"posters: all {len(set(posters))} who have posted have a vehicle set")]
    return [
        Finding(
            Level.WARN,
            f"posters: {len(without)} who have posted have no vehicle in [vehicles]: {', '.join(without)}"
            ' (fine for anyone who never says "my truck")',
        )
    ]


def exit_code(findings: Sequence[Finding]) -> int:
    """1 when anything must be fixed, else 0."""
    return 1 if any(finding.level is Level.ERROR for finding in findings) else 0
