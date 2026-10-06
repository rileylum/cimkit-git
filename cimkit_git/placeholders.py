"""Swap configured field values for placeholders and back, inside JSON entries.

Works on parsed JSON, never text: connection strings hold \\, ; and ", and only
re-serialising keeps their escaping right.
"""

import json
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Literal

from cimkit_git.config import Placeholders
from cimkit_git.errors import EntryParseError
from cimkit_git.source import entry_kind


Kind = Literal["unregistered", "unknown_key", "missing_value"]


@dataclass(frozen=True)
class Problem:
    kind: Kind
    entry: str
    text: str


def _walk(node: Any, fields: set[str], visit: Callable[[str], str | None]) -> bool:
    """Call visit on every string under a configured field; replace it when visit returns a string.

    Returns whether anything changed, so untouched entries keep their original bytes.
    """
    changed = False
    if isinstance(node, dict):
        for k, v in node.items():
            if k in fields and isinstance(v, str):
                new = visit(v)
                if new is not None and new != v:
                    node[k] = new
                    changed = True
            else:
                changed |= _walk(v, fields, visit)
    elif isinstance(node, list):
        for item in node:
            changed |= _walk(item, fields, visit)
    return changed


# A visitor gets the placeholder key (None for a real value) and the field value. It
# returns the replacement, None to keep the value, or (kind, text) to report a problem.
Visit = Callable[[str | None, str], "str | tuple[Kind, str] | None"]


def _apply(
    entries: Mapping[str, bytes], placeholders: Placeholders, visit: Visit
) -> tuple[dict[str, bytes], list[Problem]]:
    fields = set(placeholders.fields)
    prefix, suffix = placeholders.format.split("{key}")
    pattern = re.compile(re.escape(prefix) + "([a-z0-9_]+)" + re.escape(suffix))
    out = dict(entries)
    problems: dict[Problem, None] = {}  # a dict keeps first-seen order and drops repeats
    for name, data in entries.items():
        if entry_kind(data) != "json":
            continue
        try:
            obj = json.loads(data)
        except ValueError as exc:
            raise EntryParseError(f"{name}: {exc}") from exc

        def on_value(value: str) -> str | None:
            m = pattern.fullmatch(value)
            key = m[1] if m else None
            if key is not None and key not in placeholders.keys:
                problems[Problem("unknown_key", name, key)] = None
                return None
            result = visit(key, value)
            if isinstance(result, tuple):
                problems[Problem(result[0], name, result[1])] = None
                return None
            return result

        if _walk(obj, fields, on_value):
            out[name] = json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    return out, list(problems)


def neutralise(
    entries: Mapping[str, bytes], value_to_key: Mapping[str, str], placeholders: Placeholders
) -> tuple[dict[str, bytes], list[Problem]]:
    """Replace each real value with its key's placeholder, for explode.

    Problems: unregistered (a value not in value_to_key; text is the value) and
    unknown_key (a placeholder naming an undeclared key). With an empty map this is
    the leak scan check runs over committed Source.
    """

    def visit(key: str | None, value: str) -> str | tuple[Kind, str] | None:
        if key is not None:
            return None
        if value not in value_to_key:
            return ("unregistered", value)
        # The map can come from an old record, written before a key was removed.
        if value_to_key[value] not in placeholders.keys:
            return ("unknown_key", value_to_key[value])
        return placeholders.format.replace("{key}", value_to_key[value])

    return _apply(entries, placeholders, visit)


def resolve(
    entries: Mapping[str, bytes], key_to_value: Mapping[str, str], placeholders: Placeholders
) -> tuple[dict[str, bytes], list[Problem]]:
    """Replace each placeholder with the target's value, for build.

    Problems: missing_value (a declared key the target has no value for),
    unknown_key, and unregistered (a real value already in Source). Any problem
    means the result still holds a placeholder or a stray value, so it must not
    be written.
    """

    def visit(key: str | None, value: str) -> str | tuple[Kind, str] | None:
        if key is None:
            return ("unregistered", value)
        if key in key_to_value:
            return key_to_value[key]
        return ("missing_value", key)

    return _apply(entries, placeholders, visit)
