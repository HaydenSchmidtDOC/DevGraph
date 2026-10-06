"""Escape untrusted text (paths, repo ids, file contents) for Rich console markup."""

import re

# A '[' and the run of backslashes before it; group 2 is set when the '[' opens
# something Rich would parse as a tag (its own `RE_TAGS`).
_BRACKET = re.compile(r"(\\*)\[(?=([a-z#/@][^[]*?\])?)")


def escape(text: str, *, before_tag: bool = False) -> str:
    """`text` as markup that Rich prints back exactly, on every platform.

    `rich.markup.escape` only escapes tag-like brackets, but Rich's renderer
    also drops a backslash in front of any other '[', so a Windows path such
    as `C:\\repo\\[1]` lost a separator. Before a tag-like '[' Rich halves the
    backslashes, so they are doubled and one more escapes the tag; before any
    other '[' it drops exactly one, so one is added. Backslashes elsewhere,
    trailing ones included, are literal and left alone, unless `before_tag`
    says the result is placed directly before a markup tag (as in
    `[bold]{text}[/bold]`): then trailing backslashes are doubled too.
    `escape(a) + escape(b)` is not `escape(a + b)` when `a` ends in a backslash
    and `b` starts with a tag, so callers must escape the joined value.
    """

    def sub(match: re.Match) -> str:
        backslashes, tag = match.groups()
        return (backslashes * 2 if tag is not None else backslashes) + "\\["

    escaped = _BRACKET.sub(sub, text)
    if before_tag:
        trailing = len(escaped) - len(escaped.rstrip("\\"))
        escaped += "\\" * trailing
    return escaped
