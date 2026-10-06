"""Finds and parses cimkit.toml or the [tool.cimkit.git] table in pyproject.toml."""

import re
import tomllib
from dataclasses import dataclass
from pathlib import Path

from cimkit_git.errors import ConfigError

# Names are upper-cased into CIMKIT__<TARGET>__<KEY>, so mixed case would let two names
# share one variable, and a hyphen or dot is not portable in a variable name.
NAME = re.compile(r"[a-z0-9_]+")


@dataclass(frozen=True)
class Placeholders:
    fields: tuple[str, ...]
    format: str
    keys: tuple[str, ...]
    targets: tuple[str, ...]


@dataclass(frozen=True)
class Config:
    path: Path
    placeholders: Placeholders | None


def _table(path: Path, doc: dict) -> dict | None:
    if path.name == "pyproject.toml":
        return doc.get("tool", {}).get("cimkit", {}).get("git")
    return doc.get("git")


def _strings(path: Path, ph: dict, name: str) -> tuple[str, ...]:
    value = ph.get(name)
    if not isinstance(value, list) or not value or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: placeholders.{name} must be a non-empty list of strings")
    if len(set(value)) != len(value):
        raise ConfigError(f"{path}: placeholders.{name} lists a name more than once")
    return tuple(value)


def _names(path: Path, ph: dict, name: str) -> tuple[str, ...]:
    names = _strings(path, ph, name)
    for n in names:
        if not NAME.fullmatch(n):
            raise ConfigError(f"{path}: placeholders.{name}: {n!r} must be lowercase [a-z0-9_]")
    return names


def _format(path: Path, ph: dict) -> str:
    fmt = ph.get("format")
    # "{key}" alone would make every field value read as a placeholder.
    if not isinstance(fmt, str) or fmt.count("{key}") != 1 or fmt == "{key}":
        raise ConfigError(f"{path}: placeholders.format must contain {{key}} once, with text around it")
    return fmt


def _read(path: Path) -> dict | None:
    try:
        with path.open("rb") as f:
            return _table(path, tomllib.load(f))
    except tomllib.TOMLDecodeError as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def load_config(path: Path) -> Config:
    ph = (_read(path) or {}).get("placeholders")
    if ph is None:
        return Config(path, None)
    return Config(
        path,
        Placeholders(
            fields=_strings(path, ph, "fields"),
            format=_format(path, ph),
            keys=_names(path, ph, "keys"),
            targets=_names(path, ph, "targets"),
        ),
    )


def find_config(start: Path, stop: Path) -> Path | None:
    """Walk up from start to stop, inclusive. The first file with a cimkit table wins.

    stop is the git root: a config above it belongs to another repo.
    """
    for directory in (start, *start.parents):
        for name in ("cimkit.toml", "pyproject.toml"):
            path = directory / name
            if path.is_file() and _read(path) is not None:
                return path
        if directory == stop:
            break
    return None
