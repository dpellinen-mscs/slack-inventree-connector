import json
from pathlib import Path
from typing import Any

import pytest

from stockthread.config import REDACTED, ConfigError, Settings, load_settings, parse_settings

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
        (with_changes("policy", auto_apply=["unclear"]), "policy.auto_apply.0"),
        (with_changes("policy", auto_apply=["take_to_site", "take_to_site"]), "policy.auto_apply"),
        (with_changes("policy", max_quantity=0), "policy.max_quantity"),
        (with_changes("policy", default_source={"unclear": "Warehouse"}), "policy.default_source.unclear"),
        (with_changes("policy", default_source={"take_to_site": ""}), "policy.default_source.take_to_site"),
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
    environ = {**ENV, "SLACK_SIGNING_SECRET": SIGNING_SECRET}
    settings = parse_settings(base(), environ)
    effective = settings.effective()
    dumped = json.dumps(effective) + repr(settings) + str(settings)
    for secret in SECRET_VALUES:
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
