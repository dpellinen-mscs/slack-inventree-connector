"""Settings: one TOML file for behavior, environment variables for secrets.

Settings are validated once, at startup. An unknown key, a wrong type or a
missing secret stops the connector with an error that names the setting and
never repeats the value, because the value may be a secret.
"""

from __future__ import annotations

import re
import tomllib
import urllib.parse
from collections.abc import Mapping
from pathlib import Path
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, SecretStr, ValidationError, field_validator, model_validator

from stockthread.llm.openai_compatible import check_endpoint

REDACTED = "***"
_TOML_POSITION = re.compile(r"\(at (line \d+, column \d+)\)")
_SECRET_TEXT = re.compile(r"[\x21-\x7e]+")

MovementType = Literal["take_to_vehicle", "take_to_site", "return", "transfer", "receive", "consume", "count"]
"""Movement types a policy can name. `unclear` is not one: it never applies."""

SlackChannelId = Annotated[str, Field(pattern=r"^[CG][A-Z0-9]{2,}$")]
SlackUserId = Annotated[str, Field(pattern=r"^[UW][A-Z0-9]{2,}$")]
LocationPath = Annotated[str, Field(min_length=1, max_length=250, pattern=r"^\S(.*\S)?$")]
"""A stock location as InvenTree shows it: its names from the root, joined with "/"."""

# Environment variable -> (field in Secrets, required prefix of the value).
SECRET_ENV: dict[str, tuple[str, str]] = {
    "SLACK_BOT_TOKEN": ("slack_bot_token", "xoxb-"),
    "SLACK_APP_TOKEN": ("slack_app_token", "xapp-"),
    "SLACK_SIGNING_SECRET": ("slack_signing_secret", ""),
    "INVENTREE_TOKEN": ("inventree_token", ""),
    "LLM_API_KEY": ("llm_api_key", ""),
}


class ConfigError(Exception):
    """The settings are invalid. The message is safe to log: it never contains a value."""


class _Section(BaseModel):
    # Strict: "10" is not an int and "true" is not a bool. Tuple fields opt out, because TOML arrays arrive as lists.
    model_config = ConfigDict(extra="forbid", frozen=True, strict=True)


class SlackSettings(_Section):
    transport: Literal["socket", "http"] = "socket"
    workspace_id: str = Field(pattern=r"^T[A-Z0-9]{2,}$")
    """A workspace id. Enterprise Grid org installs (E...) are not supported yet."""
    app_id: str = Field(pattern=r"^A[A-Z0-9]{2,}$")
    channels: tuple[SlackChannelId, ...] = Field(default=(), strict=False)
    """Channel allowlist. Empty means the connector acts nowhere."""
    approvers: tuple[SlackUserId, ...] = Field(default=(), strict=False)
    """Slack user ids that may approve. Empty means nobody can approve."""
    catch_up_hours: int = Field(default=72, ge=0, le=24 * 30)
    group_window_seconds: int = Field(default=5, ge=0, le=300)
    plan_expiry_hours: int = Field(default=24, ge=1, le=24 * 30)


class InvenTreeSettings(_Section):
    url: str = Field(pattern=r"^https?://[^\s/@?#]+(/[^\s?#]*)?$")
    """Base URL of the InvenTree server. Credentials and a query are refused: they would be logged."""
    timeout_seconds: int = Field(default=10, ge=1, le=300)

    @field_validator("url")
    @classmethod
    def _has_host(cls, value: str) -> str:
        # urlsplit and .port raise errors that quote the value, so each one becomes a fixed message.
        try:
            parts = urllib.parse.urlsplit(value)
        except ValueError:
            raise ValueError("has an invalid host") from None
        if not parts.hostname:
            raise ValueError("has no host name")
        try:
            port = parts.port
        except ValueError:
            raise ValueError("has an invalid port") from None
        if port == 0:
            raise ValueError("has an invalid port")
        return value


class WriteSettings(_Section):
    enabled: bool = False
    """The switch of the write gate. Off on a fresh install."""


class PolicySettings(_Section):
    auto_apply: tuple[MovementType, ...] = Field(default=("take_to_vehicle", "take_to_site"), strict=False)
    """Movement types that apply without a click when they have no review reason. ADR 0004."""
    max_quantity: int = Field(default=100, ge=1)
    """A larger quantity is a review reason."""
    default_source: dict[MovementType, Annotated[str, Field(min_length=1)]] = Field(default_factory=dict)
    """Default source location per movement type. Unset means an implied source is a review reason."""

    @field_validator("auto_apply")
    @classmethod
    def _no_duplicates(cls, value: tuple[str, ...]) -> tuple[str, ...]:
        if len(set(value)) != len(value):
            raise ValueError("lists a movement type twice")
        return value


class ApplySettings(_Section):
    settle_seconds: int = Field(default=120, ge=1)
    """How long an unknown outcome is quarantined before recovery looks at it."""


class UndoSettings(_Section):
    max_age_hours: int = Field(default=7 * 24, ge=1)


class StorageSettings(_Section):
    database: str = Field(default="stockthread.db", min_length=1)


class LLMSettings(_Section):
    """The model that reads the posts the rule-based extractor declines. No `[llm]` section: no post ever
    leaves the machine, and those posts get no reply."""

    base_url: str = Field(min_length=1)
    """An OpenAI-compatible endpoint, such as "http://localhost:11434/v1" for a local server."""
    model: str = Field(min_length=1)
    timeout_seconds: int = Field(default=30, ge=1, le=120)
    """For a whole call. A post whose call fails is tried again, and after 15 minutes left unanswered."""


class Secrets(_Section):
    slack_bot_token: SecretStr
    slack_app_token: SecretStr | None = None
    slack_signing_secret: SecretStr | None = None
    inventree_token: SecretStr
    llm_api_key: SecretStr | None = None
    """Optional: a local server needs none. Unused without an `[llm]` section."""


class Settings(_Section):
    slack: SlackSettings
    inventree: InvenTreeSettings
    writes: WriteSettings = WriteSettings()
    policy: PolicySettings = PolicySettings()
    apply: ApplySettings = ApplySettings()
    undo: UndoSettings = UndoSettings()
    storage: StorageSettings = StorageSettings()
    llm: LLMSettings | None = None
    vehicles: dict[SlackUserId, LocationPath] = Field(default_factory=dict)
    """Slack user id -> the path of the stock location that is this person's vehicle ("my truck")."""
    secrets: Secrets

    @model_validator(mode="after")
    def _consistent(self) -> Settings:
        if self.slack.transport == "socket" and self.secrets.slack_app_token is None:
            raise ValueError("Socket Mode needs SLACK_APP_TOKEN")
        if self.slack.transport == "http" and self.secrets.slack_signing_secret is None:
            raise ValueError("HTTP mode needs SLACK_SIGNING_SECRET")
        if self.apply.settle_seconds <= self.inventree.timeout_seconds:
            raise ValueError("apply.settle_seconds must be longer than inventree.timeout_seconds")
        if self.llm is not None:
            problem = None
            try:
                check_endpoint(self.llm.base_url, self.llm.model, self.secrets.llm_api_key)
            except ValueError as error:
                problem = f"llm: {error}"  # a fixed sentence: it quotes no value
            if problem is not None:
                raise ValueError(problem)
        return self

    def effective(self) -> dict[str, Any]:
        """The settings as plain data, with every secret replaced by `***`. Safe to log."""
        data = self.model_dump(mode="python", exclude={"secrets"})
        data["secrets"] = {
            name: (REDACTED if getattr(self.secrets, name) is not None else None) for name in Secrets.model_fields
        }
        return data

    def writes_line(self) -> str:
        """One line for the startup log that says whether stock writes are on."""
        if not self.writes.enabled:
            return "Stock writes are OFF. Every movement is a dry-run."
        auto = ", ".join(self.policy.auto_apply) or "none"
        return f"Stock writes are ON. Automatic apply for: {auto}. Everything else waits for Apply."

    def llm_line(self) -> str:
        """One line for the startup log that says whether post text is sent to a model, and where."""
        if self.llm is None:
            return "No LLM is configured: no post leaves this machine, and posts the rules cannot read get no reply."
        host = urllib.parse.urlsplit(self.llm.base_url).hostname
        return f"Posts the rules cannot read are sent to the model {self.llm.model} at {host}."


def load_settings(path: Path, environ: Mapping[str, str]) -> Settings:
    """Read the settings file and the secrets from the environment, and validate both."""
    # The messages are built outside the except blocks, so no ConfigError carries the original
    # exception as its context: a decode error can hold the whole file.
    problem = None
    try:
        data = tomllib.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        problem = f"cannot read the settings file {path}: {exc.strerror}"
    except UnicodeDecodeError:
        problem = f"the settings file {path} is not UTF-8 text"
    except tomllib.TOMLDecodeError as exc:
        # Only the position: the rest of the message can quote the file.
        position = _TOML_POSITION.search(str(exc))
        where = f" ({position.group(1)})" if position else ""
        problem = f"the settings file {path} is not valid TOML{where}"
    if problem is not None:
        raise ConfigError(problem)
    return parse_settings(data, environ)


def parse_settings(data: Mapping[str, Any], environ: Mapping[str, str]) -> Settings:
    if "secrets" in data:
        raise ConfigError("secrets: put secrets in environment variables, not in the settings file")
    problems: dict[str, str] = {}
    secrets: dict[str, str] = {}
    for env_name, (field, prefix) in SECRET_ENV.items():
        value = environ.get(env_name, "")
        if not value:
            continue
        # A control character would end up in an HTTP header error, which quotes the header value.
        if not _SECRET_TEXT.fullmatch(value):
            problems[env_name] = f"{env_name}: does not look valid (it must be printable ASCII without spaces)"
            continue
        if not value.startswith(prefix):
            problems[env_name] = f"{env_name}: does not look valid (it must start with {prefix!r})"
            continue
        secrets[field] = value
    try:
        settings = Settings.model_validate({**data, "secrets": secrets})
    except ValidationError as exc:
        for location, message in _errors(exc):
            problems.setdefault(location, f"{location}: {message}")
    else:
        if not problems:
            return settings
    raise ConfigError("invalid settings:\n  " + "\n  ".join(problems.values()))


def _printable(text: str) -> str:
    return "".join(char if char.isprintable() else repr(char)[1:-1] for char in text)


def _errors(exc: ValidationError) -> list[tuple[str, str]]:
    """Locations and messages of a validation error. Keys from the file are shown, values never are."""
    found = []
    for error in exc.errors(include_url=False, include_input=False, include_context=False):
        # A quoted TOML key can hold anything, even a line break, so control characters are escaped.
        location = ".".join(_printable(str(part)) for part in error["loc"] if part != "[key]")
        field = location.removeprefix("secrets.")
        location = next((name for name, (secret, _) in SECRET_ENV.items() if secret == field), location)
        found.append((location or "settings", error["msg"]))
    return found
