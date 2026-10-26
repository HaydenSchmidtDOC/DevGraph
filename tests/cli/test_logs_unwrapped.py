"""`devgraph logs` writes each line whole (audit 10, item 5): with stdout not a
terminal the console folded lines at 80 columns, so `devgraph logs | grep`
missed a path split across two lines."""

from unittest.mock import MagicMock, patch

from typer.testing import CliRunner

from devgraph.cli import main as cli_main

LINE = (
    "2026-10-18 09:00:05,006 INFO devgraph.watcher: indexed 1 file(s) "
    "(src/very_long_package_name_for_testing/another_quite_long_subpackage_name/module_file.py)"
)


def test_a_long_log_line_is_not_folded(tmp_path, monkeypatch):
    # What a redirected stdout gives Rich: 80 columns (tests/conftest.py
    # widens the console to 1000 for every other test).
    monkeypatch.setenv("COLUMNS", "80")
    monkeypatch.setattr(cli_main.console, "_width", 80)
    log_path = tmp_path / "devgraph.log"
    log_path.write_text(LINE + "\n", encoding="utf-8")
    settings = MagicMock()
    settings.log_file = log_path
    with patch.object(cli_main, "get_settings", return_value=settings):
        result = CliRunner().invoke(cli_main.app, ["logs"])
    assert result.exit_code == 0, result.output
    assert result.output.splitlines() == [LINE]
