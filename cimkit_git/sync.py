"""Decides what a project needs, from the binary, the Source, the index and the record."""

import contextlib
import enum
import fnmatch
import os
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import NamedTuple

from cimkit_git import config, git, placeholders, replace, source, state
from cimkit_git.errors import CimkitError, ConfigError, NotAProjectError, PlaceholderError, RefusedError
from cimkit_git.formats import zip_json
from cimkit_git.state import Record
from cimkit_git.values import load_values

SOURCE_SUFFIX = ".aprx.src"


class Status(enum.Enum):
    CLEAN = "clean"
    BINARY_EDITED = "binary edited"
    SOURCE_CHANGED = "Source changed"
    CONFLICT = "conflict"
    NOT_BUILT = "not built"
    NEW_PROJECT = "new project"
    NO_RECORD = "no record"
    LINE_ENDINGS = "git converts line endings in Source"
    UNMERGED = "unmerged"
    STAGED_EDIT = "staged edit"


# The statuses where replacing Source loses nothing. No record counts: the design's way
# out of it is to run explode.
EXPLODES = {Status.CLEAN, Status.BINARY_EDITED, Status.NEW_PROJECT, Status.NO_RECORD}
FORCES = {Status.SOURCE_CHANGED, Status.CONFLICT}

# The statuses where replacing the binary loses nothing.
BUILDS = {Status.CLEAN, Status.SOURCE_CHANGED, Status.NOT_BUILT}
DISCARDS = {Status.BINARY_EDITED, Status.CONFLICT, Status.NO_RECORD}


@dataclass(frozen=True)
class Facts:
    binary_hash: str | None
    source_tree: str | None
    index_tree: str | None
    head_tree: str | None
    record: Record | None
    exploded_tree: Callable[[], str | None]
    unmerged: bool
    converts_text: bool


def decide(facts: Facts) -> Status:
    # Trees hashed from bytes git may have converted would compare unequal for no edit.
    if facts.converts_text:
        return Status.LINE_ENDINGS
    if facts.unmerged:
        return Status.UNMERGED
    recorded = facts.record.source_tree if facts.record else None
    # An index matching HEAD holds nothing that isn't committed, so nothing is lost.
    if facts.index_tree is not None and facts.index_tree not in (facts.source_tree, recorded, facts.head_tree):
        return Status.STAGED_EDIT
    if facts.binary_hash is None:
        return Status.NOT_BUILT
    if facts.source_tree is None:
        return Status.NEW_PROJECT
    record = facts.record
    if record is None:
        # Both sides exist but nothing says which is newer, so either write could lose an edit.
        return Status.NO_RECORD
    # Short-circuits: exploding is the slow path, needed only when the hash differs.
    binary_changed = facts.binary_hash != record.binary_hash and facts.exploded_tree() != record.source_tree
    source_changed = facts.source_tree != record.source_tree
    if binary_changed and source_changed:
        return Status.CONFLICT
    if binary_changed:
        return Status.BINARY_EDITED
    if source_changed:
        return Status.SOURCE_CHANGED
    return Status.CLEAN


class _Seen(NamedTuple):
    """Facts, plus the entries and Source files they came from, so a write uses exactly
    what the decision saw."""

    facts: Facts
    entries: dict[str, bytes] | None
    files: dict[str, bytes] | None


def find_projects(base: Path, exclude: tuple[str, ...]) -> list[Path]:
    """Every project under base, as its binary's path, whether the binary or its Source exists.

    exclude holds fnmatch globs over the binary's path relative to base, so * also
    matches across directories.
    """
    found = set()
    for root, dirs, files in os.walk(base):
        # Hidden dirs hold .git and virtualenvs; Source dirs hold no projects.
        found.update(Path(root, name.removesuffix(".src")) for name in dirs if name.endswith(SOURCE_SUFFIX))
        dirs[:] = [name for name in dirs if not name.startswith(".") and not name.endswith(SOURCE_SUFFIX)]
        found.update(Path(root, name) for name in files if name.endswith(".aprx"))
    return sorted(p for p in found if not _excluded(p.relative_to(base).as_posix(), exclude))


def _excluded(binary: str, exclude: tuple[str, ...]) -> bool:
    return any(fnmatch.fnmatchcase(binary, g) for g in exclude)


def find_sources(paths: Iterable[str], exclude: tuple[str, ...]) -> list[str]:
    """The Source dirs among file paths relative to the config dir, by find_projects' rules.

    For a commit, where only Source is committed, so binaries never count.
    """
    found = set()
    for path in paths:
        for i, segment in enumerate(path.split("/")[:-1]):
            if segment.endswith(SOURCE_SUFFIX):
                found.add("/".join(path.split("/")[: i + 1]))
                break
            if segment.startswith("."):
                break
    return sorted(s for s in found if not _excluded(s.removesuffix(".src"), exclude))


def source_dir(binary: Path) -> Path:
    return binary.with_name(binary.name + ".src")


class Workspace:
    """One worktree's config, values and sync state, found from a starting directory."""

    def __init__(self, start: Path, environ: Mapping[str, str]):
        self.root = git.repo_root(start)
        self.state_path = git.git_dir(start) / "cimkit" / "state.json"
        self._start, self._environ = start, environ
        self.reload()
        self.state = state.load(self.state_path)

    def reload(self) -> None:
        """Read config and values again, after registering a value changed them."""
        self.config = config.discover(self._start.resolve(), self.root)
        self.local_path = self.config.path.parent / "cimkit.local.toml"
        self.values = None
        if self.config.placeholders:
            self.values = load_values(self.config.placeholders, self.local_path, self._environ)

    @contextlib.contextmanager
    def locked(self) -> Iterator[None]:
        """Hold the tool lock for a run of writes. State is read again under it, so a
        write never builds on what another run has since replaced."""
        with state.lock(self.state_path.with_name("lock")):
            self.state = state.load(self.state_path)
            yield

    def projects(self) -> list[Path]:
        return find_projects(self.config.path.parent, self.config.exclude)

    def project(self, path: Path) -> Path:
        """The binary path of the project that path names, by its binary or its Source."""
        path = path.resolve()
        binary = path.with_name(path.name.removesuffix(".src")) if path.name.endswith(SOURCE_SUFFIX) else path
        if binary not in self.projects():
            raise NotAProjectError(f"{path} is not a project under {self.config.path.parent}")
        return binary

    def name(self, binary: Path) -> str:
        return binary.relative_to(self.root).as_posix()

    def explode(self, entries: Mapping[str, bytes], record: Record | None) -> dict[str, bytes]:
        """Binary entries -> Source files, in memory. Raises PlaceholderError on any problem."""
        ph = self.config.placeholders
        if ph:
            entries, problems = placeholders.neutralise(entries, self._explode_map(record), ph)
            if problems:
                raise PlaceholderError(problems)
        return source.render(entries)

    def _explode_map(self, record: Record | None) -> dict[str, str]:
        return state.explode_map(self.state.salt, record, self.values) if self.values else {}

    def _mapping(self, value_to_key: Mapping[str, str]) -> dict[str, str]:
        return {state.hash_value(self.state.salt, value): key for value, key in value_to_key.items()}

    def observe(self, binary: Path) -> Facts:
        return self._read(binary).facts

    def _read(self, binary: Path) -> _Seen:
        src = source_dir(binary)
        rel_src = self.name(src)
        record = self.state.records.get(self.name(binary))
        entries = zip_json.read(binary) if binary.exists() else None
        files = source.read_dir(src) if src.is_dir() else None

        def exploded_tree() -> str | None:
            if entries is None:
                return None
            try:
                return git.files_tree(self.root, self.explode(entries, record))
            except CimkitError:
                return None

        facts = Facts(
            binary_hash=source.entries_hash(entries) if entries is not None else None,
            source_tree=git.files_tree(self.root, files) if files is not None else None,
            index_tree=git.index_tree(self.root, rel_src),
            head_tree=git.head_tree(self.root, rel_src),
            record=record,
            exploded_tree=exploded_tree,
            unmerged=bool(git.unmerged(self.root, rel_src)),
            # Any path under Source will do: the line install writes covers them all.
            converts_text=git.converts_text(self.root, f"{rel_src}/GISProject.json"),
        )
        return _Seen(facts, entries, files)

    def status(self, binary: Path) -> Status:
        return decide(self.observe(binary))

    def _target(self, requested: str | None, record: Record | None) -> str | None:
        # The record says which target the next sync builds for, so it is never guessed
        # from the values: targets can share them.
        if requested is None:
            return record.target if record else self.values.default_target if self.values else None
        if not self.values:
            raise ConfigError(f"{self.config.path} declares no targets")
        self.values.for_target(requested)
        return requested

    def _read_to_write(self, binary: Path) -> _Seen:
        # A run that stopped mid-swap can leave Source moved aside; deciding before it is
        # back would see a new project.
        replace.recover_dir(source_dir(binary))
        return self._read(binary)

    def explode_project(self, binary: Path, *, force: bool = False, target: str | None = None) -> Record:
        """Binary -> Source. force overwrites Source edits made since the record; target
        replaces the recorded target."""
        seen = self._read_to_write(binary)
        status = decide(seen.facts)
        if status not in EXPLODES and not (force and status in FORCES):
            raise RefusedError(status)
        return self._write_source(binary, seen, target)

    def build_project(self, binary: Path, *, target: str | None = None, discard: bool = False) -> Record:
        """Source -> binary, for target, else the recorded target, else the default.
        discard overwrites binary edits made since the record, and a binary with no record."""
        seen = self._read_to_write(binary)
        status = decide(seen.facts)
        if status not in BUILDS and not (discard and status in DISCARDS):
            raise RefusedError(status)
        return self._write_binary(binary, seen, target)

    def sync_project(self, binary: Path) -> tuple[str, Record | None]:
        """The one write the status calls for: ("exploded" | "built", record), or ("clean", None)."""
        seen = self._read_to_write(binary)
        status = decide(seen.facts)
        if status in (Status.BINARY_EDITED, Status.NEW_PROJECT):
            return "exploded", self._write_source(binary, seen, None)
        if status in (Status.SOURCE_CHANGED, Status.NOT_BUILT):
            return "built", self._write_binary(binary, seen, None)
        if status is Status.CLEAN:
            return "clean", None
        raise RefusedError(status)

    def _write_source(self, binary: Path, seen: _Seen, target: str | None) -> Record:
        facts, entries = seen.facts, seen.entries
        assert entries is not None and facts.binary_hash is not None
        target = self._target(target, facts.record)
        try:
            files = self.explode(entries, facts.record)
        except PlaceholderError as exc:
            raise PlaceholderError(exc.problems, target) from None
        src = source_dir(binary)

        def unchanged() -> None:
            if (git.files_tree(self.root, source.read_dir(src)) if src.is_dir() else None) != facts.source_tree:
                raise RefusedError(Status.SOURCE_CHANGED)

        replace.replace_dir(src, files, unchanged)
        record = Record(
            git.files_tree(self.root, files),
            facts.binary_hash,
            target,
            self._mapping(self._explode_map(facts.record)),
        )
        return self._save(binary, record)

    def _write_binary(self, binary: Path, seen: _Seen, target: str | None) -> Record:
        facts, files = seen.facts, seen.files
        assert files is not None and facts.source_tree is not None
        target = self._target(target, facts.record)
        entries = self.build(files, target)

        def unchanged() -> None:
            if (source.entries_hash(zip_json.read(binary)) if binary.exists() else None) != facts.binary_hash:
                raise RefusedError(Status.BINARY_EDITED)

        replace.replace_file(binary, lambda path: zip_json.write(entries, path), unchanged)
        used = self.values.for_target(target) if self.values and target else {}
        record = Record(
            facts.source_tree,
            source.entries_hash(entries),
            target,
            self._mapping({value: key for key, value in used.items()}),
        )
        return self._save(binary, record)

    def build_artifact(self, binary: Path, out: Path, *, target: str | None = None) -> str | None:
        """Source -> out, for a deploy. Takes no lock: it changes no binary and no record.
        Returns the target built for."""
        src = source_dir(binary)
        if not src.is_dir():
            raise RefusedError(Status.NEW_PROJECT)
        target = self._target(target, self.state.records.get(self.name(binary)))
        entries = self.build(source.read_dir(src), target)
        replace.replace_file(out, lambda path: zip_json.write(entries, path), lambda: None)
        return target

    def build(self, files: Mapping[str, bytes], target: str | None) -> dict[str, bytes]:
        """Source files -> binary entries for target, in memory. Raises PlaceholderError on any problem."""
        entries = source.parse(files)
        ph = self.config.placeholders
        if ph and self.values and target:
            entries, problems = placeholders.resolve(entries, self.values.for_target(target), ph)
            if problems:
                raise PlaceholderError(problems)
        return entries

    def _save(self, binary: Path, record: Record) -> Record:
        self.state.records[self.name(binary)] = record
        state.save(self.state, self.state_path)
        return record
