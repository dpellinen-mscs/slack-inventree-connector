from pathlib import Path

import pytest

from stockthread import __main__ as cli
from stockthread import __version__


def test_version(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exit_info:
        cli.main(["--version"])
    assert exit_info.value.code == 0
    assert capsys.readouterr().out.strip() == f"stockthread {__version__}"


def test_no_arguments_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert cli.main([]) == 0
    assert "usage: stockthread" in capsys.readouterr().out


CONFIG = """
[slack]
workspace_id = "T0ACME01"
app_id = "A0STOCK1"

[inventree]
url = "http://inventree.local:8000"
"""

ENV = {
    "SLACK_BOT_TOKEN": "xoxb-1111-2222-synthetic-bot-token",
    "SLACK_APP_TOKEN": "xapp-1-A0STOCK1-synthetic-app-token",
    "INVENTREE_TOKEN": "inv-synthetic-token-0123456789",
}


@pytest.fixture
def environ(monkeypatch: pytest.MonkeyPatch) -> None:
    for name, value in ENV.items():
        monkeypatch.setenv(name, value)
    monkeypatch.delenv("SLACK_SIGNING_SECRET", raising=False)


@pytest.mark.usefixtures("environ")
def test_check_config_prints_redacted_settings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    path = tmp_path / "stockthread.toml"
    path.write_text(CONFIG, encoding="utf-8")
    assert cli.main(["check-config", "--config", str(path)]) == 0
    out = capsys.readouterr().out
    assert "Stock writes are OFF" in out
    assert '"inventree_token": "***"' in out
    for secret in ENV.values():
        assert secret not in out


@pytest.mark.usefixtures("environ")
def test_check_config_reads_the_path_from_the_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "elsewhere.toml"
    path.write_text(CONFIG, encoding="utf-8")
    monkeypatch.setenv("STOCKTHREAD_CONFIG", str(path))
    assert cli.main(["check-config"]) == 0
    assert "Stock writes are OFF" in capsys.readouterr().out


@pytest.mark.usefixtures("environ")
def test_check_config_fails_on_invalid_settings(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    path = tmp_path / "stockthread.toml"
    path.write_text(CONFIG + "\n[writes]\nenabled = 'yes'\n", encoding="utf-8")
    monkeypatch.setenv("SLACK_BOT_TOKEN", "xoxp-a-user-token-by-mistake")
    assert cli.main(["check-config", "--config", str(path)]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "SLACK_BOT_TOKEN" in captured.err
    assert "writes.enabled" in captured.err
    assert "xoxp-a-user-token-by-mistake" not in captured.err
    assert cli.main(["check-config", "--config", str(tmp_path / "missing.toml")]) == 2
