"""Sync state: what the tool last wrote or read for each project, kept in the git dir."""

import contextlib
import hashlib
import json
import secrets
import sys
from collections.abc import Iterator
from dataclasses import asdict, dataclass, field
from pathlib import Path

from cimkit_git import replace
from cimkit_git.errors import LockedError, StateError
from cimkit_git.values import Values


@dataclass(frozen=True)
class Record:
    source_tree: str
    binary_hash: str
    # None when config declares no targets.
    target: str | None
    # Salted hash of each value the build used -> its key. Never the value itself.
    mapping: dict[str, str]


@dataclass
class State:
    salt: str
    # Keyed by the binary's path relative to the repo root, in posix form.
    records: dict[str, Record] = field(default_factory=dict)


def hash_value(salt: str, value: str) -> str:
    """The salt sits beside the hashes, so it can't stop a reader of state.json testing
    a guess. It stops precomputed tables, and matching one value across repos.

    Changing this invalidates every recorded mapping.
    """
    return hashlib.sha256((salt + value).encode()).hexdigest()


def explode_map(salt: str, record: Record | None, values: Values) -> dict[str, str]:
    """Value -> key for neutralise.

    With a record, only values the last build used, under the key it used them for: a
    value that has since moved to another key must not map back to the new one.
    """
    if record is None:
        return values.value_to_key()
    return {
        value: key
        for layer in values.by_target.values()
        for key, value in layer.items()
        if record.mapping.get(hash_value(salt, value)) == key
    }


def load(path: Path) -> State:
    if not path.exists():
        return State(salt=secrets.token_hex(16))
    try:
        doc = json.loads(path.read_text())
        return State(doc["salt"], {name: Record(**rec) for name, rec in doc["records"].items()})
    except (ValueError, LookupError, TypeError) as exc:
        # The file is only a cache of what the tool last did; deleting it is safe.
        raise StateError(f"{path} is damaged ({exc}); delete it, then run explode or build --discard") from exc


def save(st: State, path: Path) -> None:
    """Replace the file whole, so a failed save leaves the previous state readable.

    Callers hold lock(): two runs saving at once would each drop the other's records.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(asdict(st), indent=2, sort_keys=True)
    replace.replace_file(path, lambda new: new.write_text(text), lambda: None)


@contextlib.contextmanager
def lock(path: Path) -> Iterator[None]:
    """Hold the tool lock, or raise LockedError at once if another process holds it.

    An OS lock rather than an O_EXCL file: the OS drops it when the process dies, so a
    crash never leaves a stale lock, and no liveness check is needed. Windows' stdlib
    has no safe one (os.kill(pid, 0) terminates the process there).
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a+b") as f:
        try:
            if sys.platform == "win32":
                import msvcrt

                # Locks one byte from the current position; it may lie past the end of the file.
                f.seek(0)
                msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl

                fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            raise LockedError("another cimkit-git is running in this worktree") from exc
        yield
