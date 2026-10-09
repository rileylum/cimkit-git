"""A target's values, from cimkit.local.toml and CIMKIT__<TARGET>__<KEY> variables."""

import tomllib
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

from cimkit_git.config import Placeholders
from cimkit_git.errors import AmbiguousValueError, ValuesError


@dataclass(frozen=True)
class Values:
    keys: tuple[str, ...]
    default_target: str
    by_target: dict[str, dict[str, str]]

    def for_target(self, target: str) -> dict[str, str]:
        if target not in self.by_target:
            raise ValuesError(f"{target!r} is not a declared target")
        return dict(self.by_target[target])

    def missing(self, target: str) -> list[str]:
        have = self.for_target(target)
        return [key for key in self.keys if key not in have]

    def value_to_key(self) -> dict[str, str]:
        """Every visible value, from every target, mapped to its key.

        Explode uses this when no record says which target the binary was built for.
        """
        out: dict[str, str] = {}
        for layer in self.by_target.values():
            for key, value in layer.items():
                if out.setdefault(value, key) != key:
                    # The value stays out of the message: it can hold a password.
                    raise AmbiguousValueError(f"{out[value]} and {key} are set to the same value")
        return out


def _read_local(placeholders: Placeholders, local: Path) -> dict:
    if not local.is_file():
        return {}
    try:
        with local.open("rb") as f:
            doc = tomllib.load(f)
    except tomllib.TOMLDecodeError as exc:
        raise ValuesError(f"{local}: {exc}") from exc
    # Undeclared names are rejected rather than ignored: a typo would otherwise
    # surface later as a missing value with nothing pointing at the cause.
    for name, table in doc.items():
        if name == "default_target":
            if table not in placeholders.targets:
                raise ValuesError(f"{local}: default_target {table!r} is not a declared target")
            continue
        if name not in placeholders.targets or not isinstance(table, dict):
            raise ValuesError(f"{local}: [{name}] is not a declared target")
        for key, value in table.items():
            if key not in placeholders.keys:
                raise ValuesError(f"{local}: [{name}] {key} is not a declared key")
            if not isinstance(value, str):
                raise ValuesError(f"{local}: [{name}] {key} must be a string")
    return doc


def load_values(placeholders: Placeholders, local: Path, environ: Mapping[str, str]) -> Values:
    """Layer the local file, then environment variables; a variable wins.

    Variables for undeclared names are ignored: CI environments carry unrelated
    CIMKIT__ variables, and only the committed config decides what a key is.
    """
    doc = _read_local(placeholders, local)
    by_target = {}
    for target in placeholders.targets:
        layer = dict(doc.get(target, {}))
        for key in placeholders.keys:
            var = f"CIMKIT__{target.upper()}__{key.upper()}"
            if var in environ:
                layer[key] = environ[var]
        by_target[target] = layer
    return Values(placeholders.keys, doc.get("default_target", placeholders.targets[0]), by_target)
