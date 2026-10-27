# SPDX-License-Identifier: MIT
"""The Slack app manifest asks for what the connector uses, and nothing more."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def manifest() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((ROOT / "manifests" / "slack-socket.json").read_text(encoding="utf-8"))
    return loaded


def test_the_scopes_are_the_minimum(manifest: dict[str, Any]) -> None:
    # Read messages in channels the app is invited to, and reply. No files, no reactions, no user token.
    assert manifest["oauth_config"]["scopes"] == {"bot": ["channels:history", "groups:history", "chat:write"]}
    assert "redirect_urls" not in manifest["oauth_config"]


def test_it_subscribes_only_to_channel_messages(manifest: dict[str, Any]) -> None:
    events = manifest["settings"]["event_subscriptions"]
    assert events == {"bot_events": ["message.channels", "message.groups"]}  # no request_url: Socket Mode


def test_it_uses_socket_mode_and_needs_no_public_url(manifest: dict[str, Any]) -> None:
    settings = manifest["settings"]
    assert settings["socket_mode_enabled"] is True
    assert settings["interactivity"] == {"is_enabled": True}  # buttons arrive over the socket too
    assert settings["token_rotation_enabled"] is False  # the bot token is read once, from the environment
    assert settings["org_deploy_enabled"] is False
    assert "http" not in json.dumps(manifest)


def test_the_quickstart_names_the_manifest_and_every_secret() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needed in ("manifests/slack-socket.json", "SLACK_BOT_TOKEN", "SLACK_APP_TOKEN", "INVENTREE_TOKEN"):
        assert needed in readme
    assert "connections:write" in readme  # the scope of the app-level token


@pytest.fixture
def http_manifest() -> dict[str, Any]:
    loaded: dict[str, Any] = json.loads((ROOT / "manifests" / "slack-http.json").read_text(encoding="utf-8"))
    return loaded


def test_the_http_manifest_differs_from_the_socket_one_only_in_the_transport(
    manifest: dict[str, Any], http_manifest: dict[str, Any]
) -> None:
    settings = http_manifest["settings"]
    url = "https://stockthread.example.com/slack/events"  # a placeholder: the README says to replace it
    assert settings["socket_mode_enabled"] is False
    assert settings["event_subscriptions"].pop("request_url") == url
    assert settings["interactivity"].pop("request_url") == url  # one path for events and clicks (slack.http.path)
    settings["socket_mode_enabled"] = True
    assert http_manifest == manifest


def test_the_quickstart_names_the_http_manifest_and_its_secret() -> None:
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    for needed in ("manifests/slack-http.json", "SLACK_SIGNING_SECRET", 'transport = "http"', "/slack/events"):
        assert needed in readme
