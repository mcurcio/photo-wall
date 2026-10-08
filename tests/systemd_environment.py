"""A systemd EnvironmentFile read the way systemd reads one (src/basic/env-file.c,
parse_env_file_internal), so a test sees exactly the values a unit's process gets: `#`/`;` comment
lines, `KEY=value` with whitespace around the key and before the value dropped; a value's
single-quoted part verbatim, its double-quoted part with `\\` escaping only `"`, `\\`, a backtick
and `$`, its unquoted part with `\\` escaping any character and trailing whitespace dropped; parts
concatenate, blanks after a quoted part dropped. No variable expansion (EnvironmentFile= does none).
"""
from __future__ import annotations

from pathlib import Path

_DOUBLE_QUOTE_ESCAPES = '"\\`$'


def read_environment_file(path: Path) -> dict[str, str]:
    environment: dict[str, str] = {}
    for key, value in _assignments(path.read_text()):
        environment[key] = value
    return environment


def _assignments(text: str):
    i, n = 0, len(text)
    while i < n:
        while i < n and text[i] in " \t\r\n":
            i += 1
        if i >= n:
            return
        if text[i] in "#;":
            while i < n and text[i] != "\n":
                i += 1
            continue
        start = i
        while i < n and text[i] not in "=\n":
            i += 1
        if i >= n or text[i] == "\n":
            continue                     # a line with no `=` is ignored
        key = text[start:i].strip()
        i = _skip_blanks(text, i + 1)
        value: list[str] = []
        trailing = 0                     # unquoted whitespace at the value's end, dropped
        while i < n and text[i] != "\n":
            c = text[i]
            if c == "'":
                end = text.index("'", i + 1)
                value.append(text[i + 1:end])
                trailing, i = 0, _skip_blanks(text, end + 1)
            elif c == '"':
                i += 1
                while text[i] != '"':
                    if text[i] == "\\" and text[i + 1] in _DOUBLE_QUOTE_ESCAPES:
                        i += 1
                    elif text[i] == "\\" and text[i + 1] == "\n":
                        i += 2
                        continue
                    value.append(text[i])
                    i += 1
                trailing, i = 0, _skip_blanks(text, i + 1)
            elif c == "\\":
                if text[i + 1] != "\n":
                    value.append(text[i + 1])
                trailing, i = 0, i + 2
            else:
                value.append(c)
                trailing = trailing + 1 if c in " \t\r" else 0
                i += 1
        joined = "".join(value)
        environment_value = joined[:len(joined) - trailing] if trailing else joined
        yield key, environment_value


def _skip_blanks(text: str, i: int) -> int:
    """Past the blanks before a value or after a quoted part (systemd's PRE_VALUE state)."""
    while i < len(text) and text[i] in " \t":
        i += 1
    return i
