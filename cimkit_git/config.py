"""Finds and parses cimkit.toml or the [tool.cimkit.git] table in pyproject.toml."""

import re
import tomllib
from collections.abc import Callable
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
    # Globs over a binary's path relative to the config file's directory.
    exclude: tuple[str, ...] = ()


def table(path: Path, doc: dict) -> dict | None:
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


# Reads a file's bytes, or None if there is none: from disk, or from a commit for check --rev.
Read = Callable[[Path], bytes | None]


def disk(path: Path) -> bytes | None:
    return path.read_bytes() if path.is_file() else None


def _read(path: Path, read: Read) -> dict | None:
    data = read(path)
    if data is None:
        return None
    try:
        return table(path, tomllib.loads(data.decode()))
    except (tomllib.TOMLDecodeError, UnicodeDecodeError) as exc:
        raise ConfigError(f"{path}: {exc}") from exc


def _exclude(path: Path, table: dict) -> tuple[str, ...]:
    value = table.get("exclude", [])
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        raise ConfigError(f"{path}: exclude must be a list of strings")
    return tuple(value)


def load_config(path: Path, read: Read = disk) -> Config:
    table = _read(path, read) or {}
    ph = table.get("placeholders")
    placeholders = None
    if ph is not None:
        placeholders = Placeholders(
            fields=_strings(path, ph, "fields"),
            format=_format(path, ph),
            keys=_names(path, ph, "keys"),
            targets=_names(path, ph, "targets"),
        )
    return Config(path, placeholders, _exclude(path, table))


def find_config(start: Path, stop: Path, read: Read = disk) -> Path | None:
    """Walk up from start to stop, inclusive. The first file with a cimkit table wins.

    stop is the git root: a config above it belongs to another repo.
    """
    for directory in (start, *start.parents):
        for name in ("cimkit.toml", "pyproject.toml"):
            path = directory / name
            if _read(path, read) is not None:
                return path
        if directory == stop:
            break
    return None


def discover(start: Path, root: Path, read: Read = disk) -> Config:
    """The config that governs start. With none, nothing is replaced and the whole repo counts."""
    found = find_config(start, root, read)
    return load_config(found, read) if found else Config(root / "cimkit.toml", None)
