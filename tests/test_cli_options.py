"""Tests that the new tuning knobs reach the query layer from both entry points.

See https://github.com/JSKenyon/MeerKhive/issues/17
"""

from typing import Any

import pytest
from typer.testing import CliRunner

from meerkhive import cli
from meerkhive.archive import DEFAULT_PAGE_SIZE, DEFAULT_PAGE_TIMEOUT, query_archive

runner = CliRunner()


@pytest.fixture
def captured_kwargs(monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Replace ``query_archive`` in the CLI with a recorder returning no rows."""
    captured: dict[str, Any] = {}

    def fake_query_archive(**kwargs: Any) -> list[dict[str, Any]]:
        captured.update(kwargs)
        return []

    monkeypatch.setattr(cli, "query_archive", fake_query_archive)
    return captured


def test_cli_defaults_to_the_tuned_page_size_and_page_timeout(
    captured_kwargs: dict[str, Any],
) -> None:
    result = runner.invoke(cli.app, [])

    assert result.exit_code == 0
    assert captured_kwargs["page_size"] == DEFAULT_PAGE_SIZE
    assert captured_kwargs["page_timeout"] == DEFAULT_PAGE_TIMEOUT


def test_cli_forwards_page_size_and_page_timeout(captured_kwargs: dict[str, Any]) -> None:
    result = runner.invoke(cli.app, ["--page-size", "50", "--page-timeout", "30"])

    assert result.exit_code == 0
    assert captured_kwargs["page_size"] == 50
    assert captured_kwargs["page_timeout"] == pytest.approx(30.0)


def test_query_archive_rejects_a_bad_page_size_before_touching_the_network() -> None:
    # No credentials are configured in the test environment, so reaching the
    # auth layer at all would surface as some other error.
    with pytest.raises(ValueError, match="page_size"):
        query_archive(page_size=0)


@pytest.mark.parametrize(
    ("option", "value"),
    [("--page-size", "0"), ("--page-timeout", "0"), ("--limit", "0"), ("--limit", "-1")],
)
def test_cli_reports_invalid_tuning_options_as_usage_errors(option: str, value: str) -> None:
    # A bad flag should read as a usage error, not a Python traceback.
    result = runner.invoke(cli.app, [option, value])

    assert result.exit_code == 2
    assert option.lstrip("-").replace("-", "_") in result.output


def test_cli_does_not_disguise_runtime_failures_as_usage_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # requests raises JSONDecodeError (a ValueError) when the auth server
    # returns an HTML error page. That is an outage, not a bad command line.
    def explode(**kwargs: Any) -> list[dict[str, Any]]:
        raise ValueError("Expecting value: line 1 column 1 (char 0)")

    monkeypatch.setattr(cli, "query_archive", explode)

    result = runner.invoke(cli.app, [])

    assert result.exit_code != 2
    assert "Invalid value" not in result.output
