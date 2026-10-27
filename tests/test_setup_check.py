# SPDX-License-Identifier: MIT
"""The setup check: roles of the token's user, `[vehicles]` paths, and posters without a vehicle (design section 5)."""

from __future__ import annotations

import os
import sqlite3
from datetime import date, timedelta
from pathlib import Path
from typing import Any

import httpx
import pytest
from pydantic import SecretStr

from stockthread import __main__ as cli
from stockthread import run
from stockthread.config import InvenTreeSettings, PolicySettings, parse_settings
from stockthread.core.matching import Alias, Catalog, LocationRecord, PartRecord, stale_aliases
from stockthread.core.model import MovementType, PostKey
from stockthread.inventree import InvenTreeError, InvenTreeReader
from stockthread.run import _stored_aliases, inventree_findings
from stockthread.setup_check import (
    DEFAULT_AUTO_APPLY,
    MINIMUM_ROLES,
    ROLE_LABELS,
    WRITE_ROLES,
    Finding,
    Level,
    check_aliases,
    check_default_sources,
    check_policy,
    check_posters,
    check_roles,
    check_sites,
    check_token,
    check_vehicles,
    exit_code,
)
from stockthread.store import AliasKind, Store, StoredAlias
from stockthread.store.db import posters_seen

NONE_OF_THE_REST = dict.fromkeys(("admin", "part_category", "bom", "build", "purchase_order", "sales_order"))
MINIMUM: dict[str, Any] = {
    "roles": {**NONE_OF_THE_REST, "part": ["view"], "stock_location": ["view"], "stock": ["view", "add", "change"]},
    "is_staff": False,
    "is_superuser": False,
}
"""As measured on InvenTree 1.5.6 for a user with the documented minimum (`test_char_roles.py`)."""
ALL = ["view", "add", "change", "delete"]
ADMIN: dict[str, Any] = {
    "roles": dict.fromkeys((*NONE_OF_THE_REST, "part", "stock_location", "stock"), ALL),
    "is_staff": True,
    "is_superuser": True,
}


def levels(findings: list[Finding]) -> list[Level]:
    return [finding.level for finding in findings]


def test_the_minimum_roles_pass_and_change_on_stock_is_not_reported() -> None:
    findings = check_roles(MINIMUM, writes=True)
    assert levels(findings) == [Level.OK]
    assert exit_code(findings) == 0


def test_a_missing_permission_is_an_error_that_says_what_it_breaks() -> None:
    body = {**MINIMUM, "roles": {**MINIMUM["roles"], "stock": ["view", "change"], "part": None}}
    findings = check_roles(body, writes=True)
    assert [f.text for f in findings if f.level is Level.ERROR] == [
        "roles: part lacks view, needed for dry runs (the catalog cannot be read)",
        "roles: stock lacks add, needed for writes (every stock adjustment is refused)",
    ]
    assert exit_code(findings) == 1


def test_an_admin_token_works_but_is_warned_about_once() -> None:
    findings = check_roles(ADMIN, writes=True)
    assert [f.text for f in findings] == ["roles: the token's user is a superuser; use a dedicated user"]
    assert exit_code(findings) == 0


def test_roles_beyond_the_minimum_are_named() -> None:
    texts = [f.text for f in check_roles({**ADMIN, "is_staff": False, "is_superuser": False}, writes=True)]
    assert "roles: stock has delete, more than needed" in texts
    assert "roles: admin has add, change, delete, view, more than needed" in texts
    assert Level.ERROR not in levels(check_roles({**ADMIN, "is_superuser": False}, writes=True))


def test_staff_alone_is_warned_about() -> None:
    texts = [f.text for f in check_roles({**MINIMUM, "is_staff": True}, writes=True)]
    assert texts == ["roles: the token's user is staff; use a dedicated user"]


@pytest.mark.parametrize("body", [{}, {"roles": None}, {"roles": ["part"]}])
def test_roles_the_server_does_not_give_are_an_error(body: dict[str, Any]) -> None:
    assert levels(check_roles(body, writes=True)) == [Level.ERROR]


def test_odd_permission_values_count_as_none() -> None:
    body = {**MINIMUM, "roles": {**MINIMUM["roles"], "part": "view", "stock_location": [1, "view"]}}
    texts = [f.text for f in check_roles(body, writes=True) if f.level is Level.ERROR]
    assert texts == ["roles: part lacks view, needed for dry runs (the catalog cannot be read)"]


CATALOG = Catalog(
    parts=(),
    locations=(
        LocationRecord(1, "Vehicles", structural=True),
        LocationRecord(2, "Vehicles/Truck 1"),
        LocationRecord(3, "Vehicles/Truck 2"),
        LocationRecord(4, "Vehicles/Truck 2"),
        LocationRecord(5, "Vehicles/Vans", structural=True),
    ),
)


def test_each_vehicle_path_that_is_not_one_stock_location_says_why() -> None:
    vehicles = {
        "U0SAM0001": "Vehicles/Truck 1",
        "U0LUIS001": "Vehicles/Truck 2",
        "U0ANA0001": "Vehicles/Vans",
        "U0KIM0001": "vehicles/truck 1",
    }
    texts = [f.text for f in check_vehicles(CATALOG, vehicles) if f.level is Level.ERROR]
    assert texts == [
        'vehicles: U0ANA0001: "Vehicles/Vans" is a structural location, which cannot hold stock',
        'vehicles: U0KIM0001: "vehicles/truck 1" is not the whole path of any stock location'
        " (paths match letter for letter)",
        'vehicles: U0LUIS001: "Vehicles/Truck 2" names more than one stock location',
    ]


def test_vehicle_paths_that_all_resolve_pass_and_none_set_says_nothing() -> None:
    assert levels(check_vehicles(CATALOG, {"U0SAM0001": "Vehicles/Truck 1"})) == [Level.OK]
    assert check_vehicles(CATALOG, {}) == []


def test_each_default_source_path_that_is_not_one_stock_location_says_why() -> None:
    defaults = {"take_to_vehicle": "Vehicles/Truck 1", "take_to_site": "Vehicles/Vans", "return": "nowhere"}
    texts = [f.text for f in check_default_sources(CATALOG, defaults) if f.level is Level.ERROR]
    assert texts == [
        'policy.default_source: return: "nowhere" is not the whole path of any stock location'
        " (paths match letter for letter)",
        'policy.default_source: take_to_site: "Vehicles/Vans" is a structural location, which cannot hold stock',
    ]
    assert levels(check_default_sources(CATALOG, {"take_to_vehicle": "Vehicles/Truck 1"})) == [Level.OK]
    assert check_default_sources(CATALOG, {}) == []


def test_posters_without_a_vehicle_are_named() -> None:
    [finding] = check_posters(["U0SAM0001", "U0LUIS001", "U0ANA0001"], {"U0SAM0001": "Vehicles/Truck 1"})
    assert finding.level is Level.WARN
    assert finding.text.startswith("posters: 2 who have posted have no vehicle in [vehicles]: U0ANA0001, U0LUIS001")
    assert levels(check_posters(["U0SAM0001"], {"U0SAM0001": "x"})) == [Level.OK]
    assert levels(check_posters(None, {})) == [Level.OK]


def test_posters_are_read_without_changing_the_database(tmp_path: Path) -> None:
    path = tmp_path / "stockthread.db"
    assert posters_seen(path) is None
    assert not path.exists()  # not created by the check
    store = Store.open(path)
    store.acquire_lock("instance-a", stale_after=timedelta(minutes=1))
    for ts, user in (("1790000000.000100", "U0SAM0001"), ("1790000100.000100", "U0LUIS001")):
        store.place_message(
            "instance-a", PostKey("T0ACME0001", "C0STOCK001", ts), user_id=user, thread_ts=None, window_seconds=5
        )
    store.place_message(
        "instance-a",
        PostKey("T0ACME0001", "C0STOCK001", "1790000101.000100"),
        user_id="U0SAM0001",
        thread_ts="1790000100.000100",
        window_seconds=5,
    )
    try:
        assert posters_seen(path) == ["U0LUIS001", "U0SAM0001"]  # while the service holds it open
    finally:
        store.close()
    before = path.stat().st_mtime_ns
    os.chmod(path, 0o444)
    try:
        assert posters_seen(path) == ["U0LUIS001", "U0SAM0001"]
    finally:
        os.chmod(path, 0o644)
    assert path.stat().st_mtime_ns == before


def test_the_command_prints_findings_and_fails_on_an_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "stockthread.toml"
    path.write_text(
        '[slack]\nworkspace_id = "T0ACME01"\napp_id = "A0STOCK1"\n\n[inventree]\nurl = "http://inventree.local:8000"\n',
        encoding="utf-8",
    )
    for name, value in {
        "SLACK_BOT_TOKEN": "xoxb-1111-2222-synthetic-bot-token",
        "SLACK_APP_TOKEN": "xapp-1-A0STOCK1-synthetic-app-token",
        "INVENTREE_TOKEN": "inv-synthetic-token-0123456789",
    }.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)
    found = [Finding(Level.OK, "inventree: fine"), Finding(Level.ERROR, "roles: stock lacks add, needed for writes")]
    monkeypatch.setattr(cli, "check_setup", lambda settings: found)
    assert cli.main(["check-setup", "--config", str(path)]) == 1
    assert capsys.readouterr().out.splitlines() == [
        "OK    inventree: fine",
        "ERROR roles: stock lacks add, needed for writes",
    ]
    monkeypatch.setattr(cli, "check_setup", lambda settings: found[:1])
    assert cli.main(["check-setup", "--config", str(path)]) == 0


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


ROOT = {"server": "InvenTree", "version": "1.5.6", "apiVersion": 530}
LOCATIONS = [{"pk": 10, "pathstring": "Vehicles/Sam", "description": "", "structural": False, "parent": None}]


TOKENS: list[Any] = [
    {"id": 1, "name": "laptop", "expiry": "2027-01-15", "in_use": False, "token": "inv-****1111"},
    {"id": 2, "name": "stockthread", "expiry": "2027-10-27", "in_use": True, "token": "inv-****2222"},
]
"""`GET /api/user/tokens/` as measured (`test_char_token_expiry`): the user's tokens, the one sent `in_use`."""


def inventree(
    roles: dict[str, Any], forbidden: frozenset[str] = frozenset(), tokens: Any = None
) -> tuple[InvenTreeReader, list[str]]:
    """A reader whose server answers the setup check; paths in `forbidden` answer 403, as a missing view role does.
    `tokens` is the body of `GET /api/user/tokens/` (TOKENS by default)."""
    asked: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        path = request.url.path
        asked.append(path)
        if path in forbidden:
            return httpx.Response(403, json={"detail": "You do not have permission to perform this action."})
        if path == "/api/":
            return httpx.Response(200, json=ROOT)
        if path == "/api/user/roles/":
            return httpx.Response(200, json=roles)
        if path == "/api/user/tokens/":
            return httpx.Response(200, json=TOKENS if tokens is None else tokens)
        results = {"/api/part/": [], "/api/stock/location/": LOCATIONS}[path]
        return httpx.Response(200, json={"count": len(results), "results": results})

    client = InvenTreeReader(
        InvenTreeSettings(url="http://inventree.local:8000"),
        SecretStr("inv-synthetic"),
        transport=httpx.MockTransport(handler),
    )
    return client, asked


@pytest.mark.anyio
async def test_a_missing_catalog_view_prints_the_roles_error_and_skips_the_catalog() -> None:
    roles = {**MINIMUM, "roles": {**MINIMUM["roles"], "part": None}}
    client, asked = inventree(roles, forbidden=frozenset({"/api/part/"}))
    async with client:
        findings = await inventree_findings(client, {"U0SAM001": "Vehicles/Sam"}, writes=True)
    assert [(f.level, f.text) for f in findings[2:]] == [
        (Level.ERROR, "roles: part lacks view, needed for dry runs (the catalog cannot be read)"),
        (
            Level.WARN,
            "location paths and aliases: not checked until part and stock_location have view",
        ),
    ]
    assert "/api/part/" not in asked
    assert exit_code(findings) == 1


@pytest.mark.anyio
async def test_a_catalog_refused_despite_the_roles_is_an_error_and_the_roles_still_print() -> None:
    client, _ = inventree(MINIMUM, forbidden=frozenset({"/api/stock/location/"}))
    async with client:
        findings = await inventree_findings(client, {"U0SAM001": "Vehicles/Sam"}, writes=True)
    assert levels(findings) == [Level.OK, Level.OK, Level.OK, Level.ERROR]
    assert findings[3].text.startswith("location paths and aliases: not checked, the catalog could not be read: ")


@pytest.mark.anyio
async def test_a_readable_catalog_checks_the_vehicle_paths() -> None:
    client, _ = inventree(MINIMUM)
    async with client:
        findings = await inventree_findings(client, {"U0SAM001": "Vehicles/Sam"}, writes=True)
    assert findings[3] == Finding(Level.OK, "vehicles: every one of the 1 paths is a stock location")


def test_the_token_expiry_warns_within_30_days_and_errs_once_past() -> None:
    today = date(2027, 9, 1)
    assert check_token(None, today) == Finding(Level.OK, "token: never expires")
    assert check_token(date(2027, 10, 2), today) == Finding(Level.OK, "token: expiry date 2027-10-02, in 31 days")
    assert check_token(date(2027, 10, 1), today) == Finding(
        Level.WARN, "token: expiry date 2027-10-01, in 30 days; create a new one before then"
    )
    assert check_token(today, today) == Finding(
        Level.WARN, "token: expiry date 2027-09-01, today; create a new one before then"
    )
    assert check_token(date(2027, 8, 31), today) == Finding(
        Level.ERROR, "token: its expiry date 2027-08-31 has passed; create a new one"
    )


@pytest.mark.anyio
async def test_the_setup_check_reports_the_expiry_of_the_token_in_use_only() -> None:
    client, asked = inventree(MINIMUM)
    async with client:
        findings = await inventree_findings(client, {}, writes=True, today=date(2027, 10, 20))
    assert findings[1] == Finding(Level.WARN, "token: expiry date 2027-10-27, in 7 days; create a new one before then")
    assert "/api/user/token/" not in asked  # that endpoint creates a token
    assert exit_code(findings) == 0


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("tokens", "expected"),
    [
        ([{**TOKENS[1], "expiry": None}], None),
        ([{**TOKENS[1], "expiry": "2028-02-29"}], date(2028, 2, 29)),
        ({"count": 2, "results": TOKENS}, "did not return a list"),
        ([TOKENS[0]], "marked 0 tokens in use, not 1"),
        ([TOKENS[1], {**TOKENS[0], "in_use": True}], "marked 2 tokens in use, not 1"),
        ([{**TOKENS[1], "in_use": "true"}], "marked 0 tokens in use, not 1"),
        ([{**TOKENS[1], "expiry": "next year"}], "no valid expiry date"),
        ([{**TOKENS[1], "expiry": 20271027}], "no valid expiry date"),
        ([{k: v for k, v in TOKENS[1].items() if k != "expiry"}], "no valid expiry date"),
    ],
)
async def test_the_reader_takes_the_expiry_of_the_one_token_in_use(tokens: Any, expected: date | str | None) -> None:
    client, _ = inventree(MINIMUM, tokens=tokens)
    async with client:
        if isinstance(expected, str):
            with pytest.raises(InvenTreeError, match=expected):
                await client.token_expiry()
        else:
            assert await client.token_expiry() == expected


@pytest.mark.anyio
async def test_an_unreadable_expiry_is_a_warning_and_the_rest_still_runs() -> None:
    client, _ = inventree(MINIMUM, forbidden=frozenset({"/api/user/tokens/"}))
    async with client:
        findings = await inventree_findings(client, {"U0SAM001": "Vehicles/Sam"}, writes=True)
    assert findings[1] == Finding(
        Level.WARN, "token: expiry not checked: InvenTree refused the token for GET /api/user/tokens/ (HTTP 403)"
    )
    assert findings[3] == Finding(Level.OK, "vehicles: every one of the 1 paths is a stock location")
    assert exit_code(findings) == 0


READ_ONLY: dict[str, Any] = {**MINIMUM, "roles": {**MINIMUM["roles"], "stock": ["view"]}}
"""The quickstart's token: view on parts, stock locations and stock, nothing else."""


def test_with_writes_off_a_read_only_token_passes_with_a_note() -> None:
    findings = check_roles(READ_ONLY, writes=False)
    assert [(f.level, f.text) for f in findings] == [
        (Level.OK, "roles: stock lacks add, needed only once writes are on (writes are off)")
    ]
    assert exit_code(findings) == 0


def test_with_writes_on_a_read_only_token_is_an_error() -> None:
    findings = check_roles(READ_ONLY, writes=True)
    assert [f.text for f in findings] == [
        "roles: stock lacks add, needed for writes (every stock adjustment is refused)"
    ]
    assert exit_code(findings) == 1


def test_with_writes_off_the_write_minimum_is_not_more_than_needed() -> None:
    assert [f.text for f in check_roles(MINIMUM, writes=False)] == [
        "roles: the token's user has the minimum roles and no more"
    ]


def test_with_writes_off_a_view_is_still_needed() -> None:
    body = {**MINIMUM, "roles": {**MINIMUM["roles"], "stock": None}}
    findings = check_roles(body, writes=False)
    assert [(f.level, f.text) for f in findings] == [
        (Level.ERROR, "roles: stock lacks view, needed for source selection (stock items cannot be read)"),
        (Level.OK, "roles: stock lacks add, needed only once writes are on (writes are off)"),
    ]
    assert exit_code(findings) == 1


ALIAS_CATALOG = Catalog(
    parts=(
        PartRecord(1, "AP-1", "Disc 40"),
        PartRecord(2, "KIT", "Install kit", template=True),
        PartRecord(3, "SVC", "Labour", virtual=True),
        PartRecord(4, "OLD", "Disc 30", active=False),
    ),
    locations=(LocationRecord(5, "Vehicles", structural=True), LocationRecord(6, "Vehicles/Truck 1")),
)


def stored(alias_id: int, kind: AliasKind, record: int) -> StoredAlias:
    return StoredAlias(alias_id, kind, "words from slack", record, None, frozenset(), None, "then", "then")


def test_an_alias_whose_record_cannot_be_matched_is_a_warning_with_its_id() -> None:
    part, location = AliasKind.PART, AliasKind.LOCATION
    aliases = [stored(1, part, 1), stored(2, part, 2), stored(3, part, 3), stored(4, part, 4), stored(5, part, 9)]
    aliases += [stored(6, location, 5), stored(7, location, 6), stored(8, location, 8)]
    findings = check_aliases(ALIAS_CATALOG, aliases)
    assert {f.level for f in findings} == {Level.WARN}
    assert [f.text.split(", which ")[1].split(": it is")[0] for f in findings] == [
        "is a template part (it holds no stock)",
        "is a virtual part (it holds no stock)",
        "is not an active part in InvenTree (made inactive, or deleted)",
        "is not an active part in InvenTree (made inactive, or deleted)",
        "is a structural location (it cannot hold stock)",
        "is not a stock location in InvenTree",
    ]
    assert findings[0].text == (
        "aliases: alias 2 names part 2, which is a template part (it holds no stock): it is ignored"
        " (`stockthread aliases remove 2` forgets it)"
    )
    assert "words from slack" not in "\n".join(f.text for f in findings)  # ids only, never a message's words
    # The same aliases the matcher ignores, no more and no fewer.
    catalog = Catalog(
        parts=ALIAS_CATALOG.parts,
        locations=ALIAS_CATALOG.locations,
        part_aliases=tuple(Alias("x", a.record) for a in aliases if a.kind is part),
        location_aliases=tuple(Alias("x", a.record) for a in aliases if a.kind is location),
    )
    warned = [int(f.text.split(" names ")[1].split(",")[0].split()[1]) for f in findings]
    assert warned == [alias.record for alias in stale_aliases(catalog)]
    assert exit_code(findings) == 0


def test_aliases_that_all_match_are_one_ok_line_and_none_are_no_line() -> None:
    assert check_aliases(ALIAS_CATALOG, [stored(1, AliasKind.PART, 1), stored(2, AliasKind.LOCATION, 6)]) == [
        Finding(Level.OK, "aliases: every one of the 2 names a record that can be matched")
    ]
    assert check_aliases(ALIAS_CATALOG, []) == []


@pytest.mark.anyio
async def test_the_catalog_read_checks_the_aliases() -> None:
    client, _ = inventree(MINIMUM)
    async with client:
        findings = await inventree_findings(
            client, {"U0SAM001": "Vehicles/Sam"}, writes=True, aliases=[stored(3, AliasKind.LOCATION, 10)]
        )
        missing = await inventree_findings(client, {}, writes=True, aliases=[stored(4, AliasKind.PART, 1)])
    assert findings[-1] == Finding(Level.OK, "aliases: every one of the 1 names a record that can be matched")
    assert missing[-1].text.startswith("aliases: alias 4 names part 1, which is not an active part")


def test_the_aliases_are_read_without_changing_the_database(tmp_path: Path) -> None:
    path = tmp_path / "stockthread.db"
    assert _stored_aliases(str(path)) == ((), [])
    assert not path.exists()  # nothing is created
    Store.open(path).close()
    assert _stored_aliases(str(path)) == ((), [])
    raw = sqlite3.connect(path, isolation_level=None)
    raw.execute("DROP TABLE apply_buttons")  # back to schema 20
    raw.execute("DROP TABLE aliases")
    raw.execute("ALTER TABLE audit_log DROP COLUMN alias_record")
    raw.execute("ALTER TABLE audit_log DROP COLUMN alias_kind")
    raw.execute("ALTER TABLE audit_log DROP COLUMN alias_id")
    raw.execute("PRAGMA user_version = 20")
    aliases, findings = _stored_aliases(str(path))
    assert aliases == ()
    assert [(f.level, f.text.split(":")[0]) for f in findings] == [(Level.WARN, "aliases")]
    assert raw.execute("PRAGMA user_version").fetchone() == (20,)  # not migrated
    raw.close()


def test_the_quickstart_lists_exactly_the_roles_a_dry_run_needs() -> None:
    """README step 4 is the minimum without the write roles, so check-setup passes for what it says (#152)."""
    readme = (Path(__file__).parents[1] / "README.md").read_text(encoding="utf-8")
    table = readme.split("| Role | Permissions |", 1)[1].split("\n\n", 1)[0]
    rows = [line.strip("| ").split(" | ") for line in table.strip().splitlines()[1:]]
    listed = {label: frozenset(perms.split(", ")) for label, perms in rows}
    dry_run = {ROLE_LABELS[name]: perms - WRITE_ROLES.get(name, frozenset()) for name, perms in MINIMUM_ROLES.items()}
    assert listed == dry_run
    for name, perms in WRITE_ROLES.items():
        assert all(f"*{perm}* on *{ROLE_LABELS[name]}*" in readme for perm in perms)


TAKE_OUTS = (MovementType.TAKE_TO_VEHICLE, MovementType.TAKE_TO_SITE)


def test_with_writes_off_the_policy_has_nothing_to_say() -> None:
    assert check_policy(writes=False, approvers=(), auto_apply=(*TAKE_OUTS, MovementType.CONSUME)) == []


def test_writes_on_with_no_approvers_warns_that_the_policy_still_writes() -> None:
    """Threat model T17: nobody can click Apply and only the poster can Undo, yet clear take-outs still apply."""
    assert check_policy(writes=True, approvers=(), auto_apply=TAKE_OUTS, sites=("Sites",)) == [
        Finding(
            Level.WARN,
            "approvers: none in slack.approvers, so nobody can click Apply or answer for an approver, and only"
            " the poster can Undo; the policy still applies with no click: take_to_vehicle, take_to_site",
        )
    ]
    [finding] = check_policy(writes=True, approvers=(), auto_apply=())
    assert finding.text.endswith("with no click: nothing")


def test_an_auto_apply_beyond_the_take_outs_warns() -> None:
    assert check_policy(writes=True, approvers=("U0DAN0001", "U0DAN0001"), auto_apply=TAKE_OUTS, sites=("Sites",)) == [
        Finding(Level.OK, "approvers: 1 may click Apply, and Undo on any post")
    ]
    assert check_policy(
        writes=True,
        approvers=("U0DAN0001",),
        auto_apply=(MovementType.TAKE_TO_SITE, MovementType.RETURN),
        sites=("Sites",),
    )[1:] == [
        Finding(Level.WARN, "policy: auto_apply has return, beyond the shipped take-outs: those apply with no click")
    ]


SITES_UNSET = Finding(
    Level.WARN,
    "policy: auto_apply has take_to_site, but locations.sites is unset, so every take to a site waits for Apply",
)


def test_a_take_to_a_site_in_auto_apply_with_no_sites_warns() -> None:
    """ADR 0022: with no sites, the shipped policy never writes a take to a site with no click."""
    assert check_policy(writes=True, approvers=("U0DAN0001",), auto_apply=TAKE_OUTS)[1:] == [SITES_UNSET]
    assert check_policy(writes=True, approvers=("U0DAN0001",), auto_apply=TAKE_OUTS, sites=("Sites",))[1:] == []
    without = (MovementType.TAKE_TO_VEHICLE,)
    assert check_policy(writes=True, approvers=("U0DAN0001",), auto_apply=without)[1:] == []
    assert check_policy(writes=False, approvers=(), auto_apply=TAKE_OUTS) == []


SITE_CATALOG = Catalog(
    parts=(),
    locations=(
        LocationRecord(1, "Sites", structural=True),
        LocationRecord(2, "Sites/Smith job", parent=1),
        LocationRecord(3, "Twice"),
        LocationRecord(4, "Twice"),
    ),
)


def test_each_site_path_must_name_one_location_and_a_folder_counts() -> None:
    assert check_sites(SITE_CATALOG, ["Sites", "Sites/Smith job"]) == [
        Finding(Level.OK, "locations.sites: every one of the 2 paths is a stock location")
    ]
    assert check_sites(SITE_CATALOG, ["Twice", "sites", "Sites"]) == [
        Finding(Level.ERROR, 'locations.sites: "Twice" names more than one stock location'),
        Finding(
            Level.ERROR,
            'locations.sites: "sites" is not the whole path of any stock location (paths match letter for letter)',
        ),
    ]
    assert check_sites(SITE_CATALOG, []) == []


@pytest.mark.anyio
async def test_a_readable_catalog_checks_the_site_paths() -> None:
    client, _ = inventree(MINIMUM)
    async with client:
        findings = await inventree_findings(client, {}, writes=True, sites=["Vehicles/Sam", "Sites"])
    assert findings[-1] == Finding(
        Level.ERROR,
        'locations.sites: "Sites" is not the whole path of any stock location (paths match letter for letter)',
    )


def test_the_shipped_take_outs_are_the_config_default() -> None:
    """Else the shipped default itself would warn as beyond the shipped take-outs."""
    assert PolicySettings().auto_apply == DEFAULT_AUTO_APPLY


def test_the_setup_check_reports_the_policy_first(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    async def nothing_from_inventree(*args: Any, **kwargs: Any) -> list[Finding]:
        return []

    monkeypatch.setattr(run, "inventree_findings", nothing_from_inventree)
    settings = parse_settings(
        {
            "slack": {"workspace_id": "T0ACME01", "app_id": "A0STOCK1", "channels": ["C0STOCK01"]},
            "inventree": {"url": "http://inventree.local:8000"},
            "storage": {"database": str(tmp_path / "stockthread.db")},
            "writes": {"enabled": True},
        },
        {"SLACK_BOT_TOKEN": "xoxb-synthetic", "SLACK_APP_TOKEN": "xapp-synthetic", "INVENTREE_TOKEN": "inv-synthetic"},
    )
    assert run.check_setup(settings)[0].text.startswith("approvers: none in slack.approvers")
