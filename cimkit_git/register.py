"""Register a new value: a key in the committed config, the value in cimkit.local.toml."""

import json
import re
import tomllib
from collections.abc import Iterable
from pathlib import Path

from cimkit_git import config, replace
from cimkit_git.errors import RegisterError

GDB = re.compile(r"([^\\/;=]+)\.gdb\b", re.IGNORECASE)
DATABASE = re.compile(r"\bDATABASE=([^;]+)", re.IGNORECASE)


def _name(text: str) -> str:
    return re.sub(r"[^a-z0-9_]", "_", text.lower())


def _base(value: str) -> str | None:
    m = GDB.search(value)
    if m:
        return _name(m[1]) + "_gdb"
    m = DATABASE.search(value)
    if m:
        return _name(m[1])
    return None


def suggest_key(value: str, taken: Iterable[str]) -> str | None:
    """A free key named after the value's .gdb or database, or None when it names neither."""
    base = _base(value)
    if base is None:
        return None
    taken = set(taken)
    key, n = base, 1
    while key in taken:
        n += 1
        key = f"{base}_{n}"
    return key


# Any keys array: pyproject.toml can hold other packages' tables, so each match is tried
# and only an edit whose parse equals the intended document is kept.
KEYS = re.compile(r"^[ \t]*keys[ \t]*=[ \t]*\[([^\]]*)\]", re.MULTILINE)


def _read(path: Path) -> str:
    # Bytes, not read_text: newline translation would rewrite a CRLF file's line endings.
    return path.read_bytes().decode() if path.exists() else ""


def _put(path: Path, old: str, new: str) -> None:
    def unchanged() -> None:
        if _read(path) != old:
            raise RegisterError(f"{path} changed while cimkit-git was editing it")

    replace.replace_file(path, lambda tmp: tmp.write_bytes(new.encode()), unchanged)


def add_key(path: Path, key: str) -> None:
    """Append key to placeholders.keys in the config file, keeping its comments and layout.

    A text edit, because the stdlib has no TOML writer. Raises RegisterError when no
    edit of a keys array parses to exactly the old config plus the key.
    """
    text = _read(path)
    want = tomllib.loads(text)
    table = config.table(path, want)
    # Only a config whose placeholders found an unregistered value gets here.
    assert table is not None
    table["placeholders"]["keys"].append(key)
    for m in KEYS.finditer(text):
        inside = m[1].rstrip()
        at = m.start(1) + len(inside)
        new = text[:at] + (f' "{key}",' if inside.endswith(",") else f', "{key}"') + text[at:]
        try:
            if tomllib.loads(new) == want:
                return _put(path, text, new)
        except tomllib.TOMLDecodeError:
            continue
    raise RegisterError(f"{path}: can't add {key} to placeholders.keys without rewriting the file")


def set_value(path: Path, target: str, key: str, value: str) -> None:
    """Set key under [target] in cimkit.local.toml, creating the file or table if needed.

    Raises RegisterError when the edit doesn't parse to exactly the old file plus the value.
    """
    text = _read(path)
    want = tomllib.loads(text)
    want.setdefault(target, {})[key] = value
    # A JSON string is a valid TOML basic string, escapes included.
    line = f"{key} = {json.dumps(value, ensure_ascii=False)}\n"
    header = re.search(rf"^[ \t]*\[[ \t]*{target}[ \t]*\][^\n]*\n", text, re.MULTILINE)
    if header:
        new = text[: header.end()] + line + text[header.end() :]
    else:
        new = text + ("\n" if text and not text.endswith("\n") else "") + f"[{target}]\n{line}"
    try:
        ok = tomllib.loads(new) == want
    except tomllib.TOMLDecodeError:
        ok = False
    if not ok:
        raise RegisterError(f"{path}: can't set [{target}] {key} without rewriting the file")
    _put(path, text, new)
