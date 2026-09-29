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
