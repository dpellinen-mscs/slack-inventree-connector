# SPDX-License-Identifier: MIT
import json
from pathlib import Path
from typing import Any

import pytest

from stockthread.config import (
    REDACTED,
    ConfigError,
    LLMSettings,
    Settings,
    SlackHttpSettings,
    load_settings,
    parse_settings,
)

MINIMAL = """
[slack]
workspace_id = "T0ACME01"
app_id = "A0STOCK1"

[inventree]
url = "http://inventree.local:8000"
"""

BOT_TOKEN = "xoxb-1111-2222-synthetic-bot-token"
APP_TOKEN = "xapp-1-A0STOCK1-synthetic-app-token"
SIGNING_SECRET = "synthetic-signing-secret-for-tests"
INVENTREE_TOKEN = "inv-synthetic-token-0123456789"

ENV = {"SLACK_BOT_TOKEN": BOT_TOKEN, "SLACK_APP_TOKEN": APP_TOKEN, "INVENTREE_TOKEN": INVENTREE_TOKEN}
SECRET_VALUES = (BOT_TOKEN, APP_TOKEN, SIGNING_SECRET, INVENTREE_TOKEN)


def base() -> dict[str, Any]:
    return {
        "slack": {"workspace_id": "T0ACME01", "app_id": "A0STOCK1"},
        "inventree": {"url": "http://inventree.local:8000"},
    }


def with_changes(section: str, **changes: Any) -> dict[str, Any]:
    data = base()
    data.setdefault(section, {}).update(changes)
    return data


def rejected(data: dict[str, Any], environ: dict[str, str] = ENV) -> str:
    with pytest.raises(ConfigError) as info:
        parse_settings(data, environ)
    return str(info.value)


def write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "stockthread.toml"
    path.write_text(text, encoding="utf-8")
    return path


def test_minimal_file_uses_safe_defaults(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, MINIMAL), ENV)
    assert settings.writes.enabled is False
    assert settings.slack.transport == "socket"
    assert settings.slack.channels == ()
    assert settings.slack.approvers == ()
    assert settings.policy.auto_apply == ("take_to_vehicle", "take_to_site")
    assert settings.policy.default_source == {}
    assert settings.apply.settle_seconds > settings.inventree.timeout_seconds
    assert settings.storage.retention_days == 30


FULL = """
[slack]
transport = "socket"
workspace_id = "T0ACME01"
app_id = "A0STOCK1"
channels = ["C0STOCK1", "G0PRIVATE"]
approvers = ["U0SAM001"]

[inventree]
url = "https://inventree.acme-fiber.example"
timeout_seconds = 15

[writes]
enabled = true

[policy]
auto_apply = []
max_quantity = 20
default_source = { take_to_vehicle = "Warehouse/Main" }

[apply]
settle_seconds = 180

[undo]
max_age_hours = 48

[storage]
database = "/data/stockthread.db"
retention_days = 7
"""


def test_full_file(tmp_path: Path) -> None:
    settings = load_settings(write(tmp_path, FULL), ENV)
    assert settings.slack.channels == ("C0STOCK1", "G0PRIVATE")
    assert settings.slack.approvers == ("U0SAM001",)
    assert settings.writes.enabled is True
    assert settings.policy.auto_apply == ()
    assert settings.policy.max_quantity == 20
    assert settings.policy.default_source == {"take_to_vehicle": "Warehouse/Main"}
    assert settings.apply.settle_seconds == 180
    assert settings.undo.max_age_hours == 48
    assert settings.storage.database == "/data/stockthread.db"
    assert settings.storage.retention_days == 7


@pytest.mark.parametrize(
    ("data", "where"),
    [
        (with_changes("slack", channel=["C0STOCK1"]), "slack.channel"),
        (with_changes("writes", enable=True), "writes.enable"),
        ({**base(), "extra": {}}, "extra"),
    ],
)
def test_unknown_key_is_refused(data: dict[str, Any], where: str) -> None:
    assert where in rejected(data)


@pytest.mark.parametrize(
    ("data", "where"),
    [
        (with_changes("writes", enabled="true"), "writes.enabled"),
        (with_changes("writes", enabled=1), "writes.enabled"),
        (with_changes("inventree", timeout_seconds="10"), "inventree.timeout_seconds"),
        (with_changes("slack", channels="C0STOCK1"), "slack.channels"),
        (with_changes("slack", channels=[123]), "slack.channels.0"),
        (with_changes("slack", channels=["#stock"]), "slack.channels.0"),
        (with_changes("slack", approvers=["sam"]), "slack.approvers.0"),
        (with_changes("slack", transport="websocket"), "slack.transport"),
        (with_changes("slack", workspace_id="acme"), "slack.workspace_id"),
        (with_changes("inventree", url="inventree.local"), "inventree.url"),
        (with_changes("inventree", url="https://sam:SPOT@inventree.local"), "inventree.url"),
        (with_changes("inventree", url="https://inventree.local/?token=SPOT"), "inventree.url"),
        (with_changes("inventree", url="https://inventree.local/#SPOT"), "inventree.url"),
        (with_changes("inventree", url="http://:"), "inventree.url"),
        (with_changes("inventree", url="http://:8000/"), "inventree.url"),
        (with_changes("inventree", url="http://inventree.local:SPOT"), "inventree.url"),
        (with_changes("inventree", url="http://inventree.local:99999"), "inventree.url"),
        (with_changes("inventree", url="http://inventree.local:0"), "inventree.url"),
        (with_changes("inventree", url="http://[SPOT]/"), "inventree.url"),
        (with_changes("inventree", url="http://[SPOT/"), "inventree.url"),
        (with_changes("policy", auto_apply=["unclear"]), "policy.auto_apply.0"),
        (with_changes("policy", auto_apply=["take_to_site", "take_to_site"]), "policy.auto_apply"),
        (with_changes("policy", max_quantity=0), "policy.max_quantity"),
        (with_changes("policy", default_source={"unclear": "Warehouse"}), "policy.default_source.unclear"),
        (with_changes("policy", default_source={"take_to_site": ""}), "policy.default_source.take_to_site"),
        (with_changes("policy", default_source={"receive": "Warehouse/Receiving"}), "policy.default_source"),
    ],
)
def test_wrong_value_is_refused_and_named(data: dict[str, Any], where: str) -> None:
    message = rejected(data)
    assert f"{where}:" in message
    assert "SPOT" not in message


def test_missing_required_settings_are_named() -> None:
    message = rejected({})
    assert "slack: Field required" in message
    assert "inventree: Field required" in message


@pytest.mark.parametrize("name", ["SLACK_BOT_TOKEN", "INVENTREE_TOKEN", "SLACK_APP_TOKEN"])
def test_missing_secret_is_named_by_its_variable(name: str) -> None:
    environ = {key: value for key, value in ENV.items() if key != name}
    assert name in rejected(base(), environ)


def test_http_mode_needs_a_signing_secret() -> None:
    data = with_changes("slack", transport="http")
    environ = {key: value for key, value in ENV.items() if key != "SLACK_APP_TOKEN"}
    assert "SLACK_SIGNING_SECRET" in rejected(data, environ)
    settings = parse_settings(data, {**environ, "SLACK_SIGNING_SECRET": SIGNING_SECRET})
    assert settings.slack.transport == "http"


def test_settle_time_must_exceed_the_request_timeout() -> None:
    data = with_changes("apply", settle_seconds=10)
    assert "apply.settle_seconds must be longer" in rejected(data)


SHORT = {**with_changes("storage", retention_days=4), "undo": {"max_age_hours": 24}}


@pytest.mark.parametrize(
    "data",
    [
        with_changes("storage", retention_days=7),  # undo.max_age_hours is 168 by default: not longer
        {**SHORT, "slack": {**base()["slack"], "plan_expiry_hours": 120, "catch_up_hours": 24}},
        {**SHORT, "slack": {**base()["slack"], "catch_up_hours": 120}},
    ],
)
def test_retention_must_outlast_the_plan_expiry_catch_up_and_the_undo_age_limit(data: dict[str, Any]) -> None:
    assert "storage.retention_days must be longer" in rejected(data)
    fits = {**SHORT, "slack": {**base()["slack"], "catch_up_hours": 24}}
    assert parse_settings(fits, ENV).storage.retention_days == 4


def test_secrets_are_not_accepted_from_the_file(tmp_path: Path) -> None:
    path = write(tmp_path, MINIMAL + f'\n[secrets]\ninventree_token = "{INVENTREE_TOKEN}"\n')
    with pytest.raises(ConfigError) as info:
        load_settings(path, ENV)
    assert "environment variables" in str(info.value)
    assert INVENTREE_TOKEN not in str(info.value)


@pytest.mark.parametrize(
    ("name", "value"),
    [
        ("SLACK_BOT_TOKEN", "xoxp-1111-a-user-token-by-mistake"),
        ("SLACK_APP_TOKEN", "xoxb-1111-the-bot-token-by-mistake"),
        ("INVENTREE_TOKEN", INVENTREE_TOKEN + "\n"),
    ],
)
def test_malformed_secret_is_refused_without_echoing_it(name: str, value: str) -> None:
    message = rejected(base(), {**ENV, name: value})
    assert name in message
    assert value.strip() not in message


def test_no_error_repeats_a_value() -> None:
    # Every field wrong at once, with values that would be easy to spot in the message. Keys are shown, values never.
    data = {
        "slack": {"workspace_id": "SPOT-1", "app_id": "SPOT-2", "channels": ["SPOT-3"], "transport": "SPOT-4"},
        "inventree": {"url": "SPOT-5", "timeout_seconds": "SPOT-6"},
        "writes": {"enabled": "SPOT-7"},
        "policy": {"auto_apply": ["SPOT-8"], "default_source": {"take_to_site": 9}},
    }
    message = rejected(data)
    assert "SPOT" not in message
    assert message.count("\n") >= 8


def test_unreadable_or_invalid_file(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read"):
        load_settings(tmp_path / "missing.toml", ENV)
    path = write(tmp_path, f'[slack\nworkspace_id = "{BOT_TOKEN}"\n')
    with pytest.raises(ConfigError, match="not valid TOML") as info:
        load_settings(path, ENV)
    assert BOT_TOKEN not in str(info.value)


def test_effective_settings_redact_every_secret() -> None:
    environ = {**ENV, "SLACK_SIGNING_SECRET": SIGNING_SECRET, "LLM_API_KEY": "key-synthetic"}
    settings = parse_settings(base(), environ)
    effective = settings.effective()
    dumped = json.dumps(effective) + repr(settings) + str(settings)
    for secret in (*SECRET_VALUES, "key-synthetic"):
        assert secret not in dumped
    assert effective["secrets"] == dict.fromkeys(effective["secrets"], REDACTED)
    assert effective["writes"] == {"enabled": False}


def test_effective_settings_show_an_absent_secret_as_none() -> None:
    effective = parse_settings(base(), ENV).effective()
    assert effective["secrets"]["slack_signing_secret"] is None


def test_writes_line() -> None:
    off = parse_settings(base(), ENV)
    assert off.writes_line() == "Stock writes are OFF. Every movement is a dry-run."
    on = parse_settings(with_changes("writes", enabled=True), ENV)
    assert "ON" in on.writes_line()
    assert "take_to_vehicle, take_to_site" in on.writes_line()
    review_first = parse_settings({**with_changes("writes", enabled=True), "policy": {"auto_apply": []}}, ENV)
    assert "Automatic apply for: none" in review_first.writes_line()


def test_settings_are_frozen() -> None:
    settings = parse_settings(base(), ENV)
    with pytest.raises(ValueError, match="frozen"):
        settings.writes.enabled = True  # type: ignore[misc]
    assert isinstance(settings, Settings)


def test_example_file_is_valid_and_matches_the_defaults() -> None:
    example = Path(__file__).resolve().parent.parent / "stockthread.example.toml"
    settings = load_settings(example, ENV)
    assert settings == parse_settings(base(), ENV)


def test_every_problem_is_reported_at_once() -> None:
    data = with_changes("writes", enabled="yes")
    message = rejected(data, {**ENV, "SLACK_BOT_TOKEN": "xoxp-a-user-token-by-mistake"})
    assert "SLACK_BOT_TOKEN: does not look valid" in message
    assert "writes.enabled" in message
    assert "SLACK_BOT_TOKEN: Field required" not in message


@pytest.mark.parametrize("value", [" inv-SPOT", "inv-SPOT\n", "inv-SP\nOT", "inv-SP OT", "inv-SPÖT", "inv-SP\x00OT"])
def test_a_secret_with_spaces_control_characters_or_non_ascii_is_refused(value: str) -> None:
    message = rejected(base(), {**ENV, "INVENTREE_TOKEN": value})
    assert "INVENTREE_TOKEN: does not look valid" in message
    assert "SPOT" not in message
    assert "inv-" not in message


def test_url_may_have_a_port_or_an_ip_address() -> None:
    for url in ("http://inventree.local:8000", "http://192.0.2.10/", "http://[2001:db8::10]:8000/"):
        assert parse_settings(with_changes("inventree", url=url), ENV).inventree.url == url


def test_url_may_have_a_path() -> None:
    settings = parse_settings(with_changes("inventree", url="https://tools.acme-fiber.example/inventree/"), ENV)
    assert settings.inventree.url == "https://tools.acme-fiber.example/inventree/"


def test_a_secrets_section_is_refused_by_the_parser_too() -> None:
    message = rejected({**base(), "secrets": {"inventree_token": INVENTREE_TOKEN}})
    assert "environment variables" in message
    assert INVENTREE_TOKEN not in message


def test_file_that_is_not_utf8(tmp_path: Path) -> None:
    path = tmp_path / "stockthread.toml"
    path.write_bytes(b"\xffSECRET = 1\n")
    with pytest.raises(ConfigError, match="not UTF-8") as info:
        load_settings(path, ENV)
    assert "SECRET" not in str(info.value)
    assert info.value.__context__ is None
    assert info.value.__cause__ is None


def test_toml_error_carries_no_context(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match=r"line 2, column \d+") as info:
        load_settings(write(tmp_path, f'[slack]\nworkspace_id = "{BOT_TOKEN}\n'), ENV)
    assert info.value.__context__ is None


def test_a_key_cannot_forge_a_log_line() -> None:
    message = rejected(with_changes("policy", default_source={"x\nERROR forged": "Warehouse"}))
    assert "\nERROR forged" not in message
    assert "x\\nERROR forged" in message


def test_no_llm_section_means_no_model() -> None:
    settings = parse_settings(base(), {**ENV, "LLM_API_KEY": "key-synthetic"})
    assert settings.llm is None
    assert settings.effective()["llm"] is None
    assert "no post leaves this machine" in settings.llm_line()


def test_an_llm_section_names_the_endpoint_and_the_model() -> None:
    data = {**base(), "llm": {"base_url": "https://llm.acme-fiber.example/v1", "model": "small-1"}}
    settings = parse_settings(data, {**ENV, "LLM_API_KEY": "key-synthetic"})
    assert settings.llm is not None
    assert settings.llm.timeout_seconds == 30
    assert settings.secrets.llm_api_key is not None
    assert settings.effective()["secrets"]["llm_api_key"] == REDACTED
    assert "key-synthetic" not in json.dumps(settings.effective()) + repr(settings)
    assert settings.llm_line() == "Posts the rules cannot read are sent to the model small-1 at llm.acme-fiber.example."
    local = {**base(), "llm": {"base_url": "http://localhost:11434/v1", "model": "small-1"}}
    assert parse_settings(local, ENV).secrets.llm_api_key is None  # a local server needs no key


@pytest.mark.parametrize(
    ("llm", "key", "problem"),
    [
        ({"model": "small-1"}, None, "llm.base_url"),
        ({"base_url": "https://llm.acme-fiber.example/v1"}, None, "llm.model"),
        ({"base_url": "ftp://llm.acme-fiber.example", "model": "m"}, None, "the provider URL must be http"),
        ({"base_url": "https://user:hunter2@llm.acme-fiber.example", "model": "m"}, None, "without credentials"),
        ({"base_url": "https://llm.acme-fiber.example/v1?key=hunter2", "model": "m"}, None, "without a query"),
        ({"base_url": "https://[hunter2/v1", "model": "m"}, None, "the provider URL must be http"),
        ({"base_url": "http://llm.acme-fiber.example/v1", "model": "m"}, "key-synthetic", "only sent over https"),
        ({"base_url": "http://localhost:0/v1", "model": "m"}, None, "invalid port"),
        ({"base_url": "http://localhost:99999/v1", "model": "m"}, None, "invalid port"),
        ({"base_url": "http://localhost:-1/v1", "model": "m"}, None, "invalid port"),
        ({"base_url": "https://:@llm.acme-fiber.example/v1", "model": "m"}, None, "without credentials"),
        ({"base_url": "https://llm.acme-fiber.example/v1/chat/completions", "model": "m"}, None, "the base URL"),
        ({"base_url": "https://llm.acme-fiber.example/v1", "model": " "}, None, "model name is empty"),
        ({"base_url": "https://llm.acme-fiber.example/v1", "model": "m", "timeout_seconds": 121}, None, "timeout"),
        ({"base_url": "https://llm.acme-fiber.example/v1", "model": "m", "api_key": "hunter2"}, None, "llm.api_key"),
    ],
)
def test_a_wrong_llm_section_is_refused_without_quoting_it(llm: dict[str, Any], key: str | None, problem: str) -> None:
    environ = {**ENV, "LLM_API_KEY": key} if key else ENV
    with pytest.raises(ConfigError, match=problem) as info:
        parse_settings({**base(), "llm": llm}, environ)
    assert "hunter2" not in str(info.value)
    assert "key-synthetic" not in str(info.value)
    assert info.value.__cause__ is None
    assert info.value.__context__ is None


def test_the_llm_section_of_the_example_file_is_valid_once_uncommented(tmp_path: Path) -> None:
    example = (Path(__file__).resolve().parent.parent / "stockthread.example.toml").read_text(encoding="utf-8")
    start = example.index("# [llm]")
    section = "\n".join(line.removeprefix("# ") for line in example[start:].splitlines())
    path = tmp_path / "stockthread.toml"
    path.write_text(example[:start] + section, encoding="utf-8")
    settings = load_settings(path, ENV)
    assert settings.llm is not None
    assert settings.llm.timeout_seconds == LLMSettings.model_fields["timeout_seconds"].default
    assert "at localhost" in settings.llm_line()


def test_sites_are_location_paths_none_by_default_and_none_twice() -> None:
    assert parse_settings(base(), ENV).locations.sites == ()
    sites = parse_settings({**base(), "locations": {"sites": ["Sites", "Job Sites/Elm"]}}, ENV).locations.sites
    assert sites == ("Sites", "Job Sites/Elm")
    for bad in (["Sites", "Sites"], [""], [" Sites"], "Sites"):
        assert "locations.sites" in rejected({**base(), "locations": {"sites": bad}}, ENV)


def test_vehicles_map_a_slack_user_to_a_location_path(tmp_path: Path) -> None:
    assert parse_settings(base(), ENV).vehicles == {}
    text = MINIMAL + '\n[vehicles]\nU0SAM0001 = "Vehicles/Truck 1"\nW0LUIS005 = "Vehicles/Van 3"\n'
    settings = load_settings(write(tmp_path, text), ENV)
    assert settings.vehicles == {"U0SAM0001": "Vehicles/Truck 1", "W0LUIS005": "Vehicles/Van 3"}
    assert settings.effective()["vehicles"] == settings.vehicles


@pytest.mark.parametrize(
    ("vehicles", "where"),
    [
        ({"sam": "Vehicles/SPOT"}, "vehicles.sam"),  # a name is not a Slack user id
        ({"U0SAM0001": ""}, "vehicles.U0SAM0001"),
        ({"U0SAM0001": " Vehicles/SPOT"}, "vehicles.U0SAM0001"),
        ({"U0SAM0001": "Vehicles/SPOT\n"}, "vehicles.U0SAM0001"),
        ({"U0SAM0001": "Vehicles/SPOT\nERROR forged"}, "vehicles.U0SAM0001"),
        ({"U0SAM0001": "SPOT" * 100}, "vehicles.U0SAM0001"),
        ({"U0SAM0001": 11}, "vehicles.U0SAM0001"),  # a path, not InvenTree's number
        ({"U0SAM0001": ["Vehicles/SPOT"]}, "vehicles.U0SAM0001"),
    ],
)
def test_a_wrong_vehicle_is_refused_and_named_without_its_value(vehicles: dict[str, Any], where: str) -> None:
    message = rejected({**base(), "vehicles": vehicles})
    assert where in message
    assert "SPOT" not in message
    assert "forged" not in message


@pytest.mark.parametrize("path", ["/slack/events", "/hooks/stock-thread_1", "/a.b"])
def test_an_http_path_of_plain_segments_is_accepted(path: str) -> None:
    assert SlackHttpSettings(path=path).path == path


@pytest.mark.parametrize(
    "path", ["", "/", "slack", "/slack/", "//slack", "/slack//events", "/../slack", "/./x", "/a b"]
)
def test_an_http_path_a_proxy_could_rewrite_is_refused(path: str) -> None:
    with pytest.raises(ValueError, match="path"):
        SlackHttpSettings(path=path)
