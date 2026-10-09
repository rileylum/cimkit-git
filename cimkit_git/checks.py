"""The CI gate: findings over one project's committed Source."""

import functools
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

from cimkit_git import config, git, source
from cimkit_git import placeholders as ph_
from cimkit_git.config import Config, Placeholders
from cimkit_git.errors import EntryParseError
from cimkit_git.sync import find_projects, find_sources, source_dir

# Pro runs only on Windows, so only drive and UNC forms are machine paths. POSIX forms
# would match the many slash-led strings in JSON, and URLs are excluded on purpose: the
# corpus is full of XML namespaces and public Esri services. A path runs until a
# character no Windows path holds, or a ; that ends a connection-string part.
PATH = re.compile(r'(?:(?<![A-Za-z0-9])[A-Za-z]:[\\/]|\\\\[^\\/\s"<>|]+\\)[^"<>|;*?\r\n\t]*')

Kind = ph_.Kind | Literal["parse", "path"]


@dataclass(frozen=True)
class Finding:
    kind: Kind
    entry: str
    text: str
    count: int = 1


def check(files: Mapping[str, bytes], placeholders: Placeholders | None) -> list[Finding]:
    findings = []
    parsed = {}
    for name, data in files.items():
        try:
            parsed[name] = source.strings(name, data)
        except EntryParseError as exc:
            findings.append(Finding("parse", name, str(exc.__cause__)))
    unregistered = set()
    if placeholders:
        # An empty map makes every real value in a configured field unregistered.
        _, problems = ph_.neutralise({name: files[name] for name in parsed}, {}, placeholders)
        findings += [Finding(p.kind, p.entry, p.text) for p in problems]
        unregistered = {(p.entry, p.text) for p in problems if p.kind == "unregistered"}
    for name, strings in parsed.items():
        # An unregistered value is already an error; a warning on it too would be noise.
        paths = [m[0].rstrip() for s in strings if (name, s) not in unregistered for m in PATH.finditer(s)]
        if paths:
            findings.append(Finding("path", name, paths[0], len(paths)))
    return findings


@dataclass(frozen=True)
class Committed:
    """What check reads: the working tree, or one commit."""

    root: Path
    config: Config
    # Repo-relative Source dir -> its files. Read lazily, so a project that can't be read
    # is reported and the rest are still checked.
    sources: dict[str, Callable[[], dict[str, bytes]]]


def working_tree(start: Path) -> Committed:
    root = git.repo_root(start)
    cfg = config.discover(start, root)
    dirs = [source_dir(b) for b in find_projects(cfg.path.parent, cfg.exclude)]
    sources = {d.relative_to(root).as_posix(): functools.partial(source.read_dir, d) for d in dirs if d.is_dir()}
    return Committed(root, cfg, sources)


def at_rev(start: Path, rev: str) -> Committed:
    """The commit's own config, so a commit that adds a key is checked against that key."""
    root = git.repo_root(start)
    tree = git.rev_tree(root, rev)
    cfg = config.discover(start, root, lambda path: git.show(root, tree, path.relative_to(root).as_posix()))
    base = cfg.path.parent.relative_to(root).as_posix()
    prefix = "" if base == "." else base + "/"
    dirs = [prefix + d for d in find_sources(git.tree_paths(root, tree, prefix), cfg.exclude)]
    return Committed(root, cfg, {d: functools.partial(git.tree_files, root, tree, d) for d in dirs})
