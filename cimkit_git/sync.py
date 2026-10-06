"""Decides what a project needs, from the binary, the Source, the index and the record."""

import enum
import fnmatch
import os
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path

from cimkit_git import config, git, placeholders, source, state
from cimkit_git.errors import CimkitError, PlaceholderError
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


@dataclass(frozen=True)
class Facts:
    binary_hash: str | None
    source_tree: str | None
    index_tree: str | None
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
    if facts.index_tree is not None and facts.index_tree not in (facts.source_tree, recorded):
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
    return sorted(
        p for p in found if not any(fnmatch.fnmatchcase(p.relative_to(base).as_posix(), g) for g in exclude)
    )


def source_dir(binary: Path) -> Path:
    return binary.with_name(binary.name + ".src")


class Workspace:
    """One worktree's config, values and sync state, found from a starting directory."""

    def __init__(self, start: Path, environ: Mapping[str, str]):
        self.root = git.repo_root(start)
        self.state_path = git.git_dir(start) / "cimkit" / "state.json"
        found = config.find_config(start.resolve(), self.root)
        # With no config there is nothing to replace, and projects anywhere in the repo count.
        self.config = config.load_config(found) if found else config.Config(self.root / "cimkit.toml", None)
        self.values = None
        if self.config.placeholders:
            local = self.config.path.parent / "cimkit.local.toml"
            self.values = load_values(self.config.placeholders, local, environ)
        self.state = state.load(self.state_path)

    def projects(self) -> list[Path]:
        return find_projects(self.config.path.parent, self.config.exclude)

    def name(self, binary: Path) -> str:
        return binary.relative_to(self.root).as_posix()

    def explode(self, entries: Mapping[str, bytes], record: Record | None) -> dict[str, bytes]:
        """Binary entries -> Source files, in memory. Raises PlaceholderError on any problem."""
        ph = self.config.placeholders
        if ph and self.values:
            entries, problems = placeholders.neutralise(
                entries, state.explode_map(self.state.salt, record, self.values), ph
            )
            if problems:
                raise PlaceholderError(problems)
        return source.render(entries)

    def observe(self, binary: Path) -> Facts:
        src = source_dir(binary)
        rel_src = self.name(src)
        record = self.state.records.get(self.name(binary))
        entries = zip_json.read(binary) if binary.exists() else None

        def exploded_tree() -> str | None:
            if entries is None:
                return None
            try:
                return git.files_tree(self.root, self.explode(entries, record))
            except CimkitError:
                return None

        return Facts(
            binary_hash=source.entries_hash(entries) if entries is not None else None,
            source_tree=git.files_tree(self.root, source.read_dir(src)) if src.is_dir() else None,
            index_tree=git.index_tree(self.root, rel_src),
            record=record,
            exploded_tree=exploded_tree,
            unmerged=bool(git.unmerged(self.root, rel_src)),
            # Any path under Source will do: the line install writes covers them all.
            converts_text=git.converts_text(self.root, f"{rel_src}/GISProject.json"),
        )

    def status(self, binary: Path) -> Status:
        return decide(self.observe(binary))
