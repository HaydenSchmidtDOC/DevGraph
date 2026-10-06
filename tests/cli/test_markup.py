"""`devgraph.cli.markup.escape`: untrusted text prints back exactly through Rich markup."""

import pytest
from rich.console import Console
from rich.markup import escape as rich_escape

from devgraph.cli.markup import escape

TEXTS = [
    "C:\\repo\\[x]\\a.md",
    "C:\\repo\\[1]\\a.md",
    "C:\\repo\\[X]\\[/x]\\[\\x]",
    "C:\\repo\\\\[bold]\\\\\\[1]",
    "C:\\repo\\",
    "/home/user/[x]/[/x]",
    "[/bad]",
    "[bold red]not a tag[/]",
    "[link=https://example.com]x[/link]",
    "[[x]]",
    "[",
    "]",
    "plain/path.md",
]


def printed(markup: str) -> str:
    console = Console(width=500, color_system=None, highlight=False, emoji=False)
    with console.capture() as capture:
        console.print(markup, end="")
    return capture.get()


@pytest.mark.parametrize("text", TEXTS)
def test_text_round_trips_through_markup(text):
    assert printed(escape(text)) == text


@pytest.mark.parametrize("text", TEXTS)
def test_text_round_trips_between_tags(text):
    assert printed(f"[red]A:[/red] {escape(text)}: [green]z[/green]") == f"A: {text}: z"


@pytest.mark.parametrize("text", [t for t in TEXTS if "\\" not in t])
def test_output_without_backslashes_matches_rich_escape(text):
    assert printed(escape(text)) == printed(rich_escape(text))


@pytest.mark.parametrize("text", TEXTS)
def test_text_round_trips_directly_before_a_tag(text):
    assert printed(f"[red]{escape(text, before_tag=True)}[/red]") == text
