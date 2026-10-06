"""Thin wrapper over the git commands the sync decisions need."""

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from cimkit_git.errors import NotAGitRepoError


def _git(root: Path, *args: str, stdin: bytes | None = None, env: Mapping[str, str] | None = None) -> str:
    result = subprocess.run(
        ["git", *args],
        cwd=root,
        input=stdin,
        capture_output=True,
        env={**os.environ, **env} if env else None,
        check=True,
    )
    return result.stdout.decode()


def _rev_parse(start: Path, flag: str) -> Path:
    try:
        return Path(_git(start, "rev-parse", flag).strip()).resolve()
    except subprocess.CalledProcessError as exc:
        raise NotAGitRepoError(f"{start} is not inside a git repository") from exc


def repo_root(start: Path) -> Path:
    return _rev_parse(start, "--show-toplevel")


def git_dir(start: Path) -> Path:
    """The git dir of start's worktree; a linked worktree gets its own."""
    return _rev_parse(start, "--absolute-git-dir")


def converts_text(root: Path, path: str) -> bool:
    """Whether git may rewrite line endings in path on checkout or add.

    Only an unset text attribute (-text, or binary) rules it out: anything else leaves
    it to core.autocrlf, which defaults to true on Windows.
    """
    out = _git(root, "check-attr", "-z", "text", "--", path)
    return out.split("\0")[2] != "unset"


def files_tree(root: Path, files: Mapping[str, bytes]) -> str:
    """The tree ID git would commit for these files, written into the object store.

    Blobs are hashed with --no-filters, so the ID depends on the bytes alone and never
    on core.autocrlf or attributes; the caller must make sure git doesn't convert Source.
    The objects are written so a recorded tree stays readable for a later merge.
    """
    with tempfile.TemporaryDirectory() as tmp:
        # Numbered files sidestep newlines and nesting in entry names.
        paths = []
        for i, data in enumerate(files.values()):
            path = Path(tmp, str(i))
            path.write_bytes(data)
            paths.append(str(path))
        blobs = _git(root, "hash-object", "-w", "--no-filters", "--stdin-paths", stdin="\n".join(paths).encode())
        return _tree(root, [f"100644 blob {blob}\t{name}" for name, blob in zip(files, blobs.split())])


def _tree(root: Path, index_info: list[str]) -> str:
    """Write a tree from update-index --index-info lines, through a scratch index."""
    with tempfile.TemporaryDirectory() as tmp:
        env = {"GIT_INDEX_FILE": str(Path(tmp, "index"))}
        info = "".join(line + "\0" for line in index_info).encode()
        _git(root, "update-index", "-z", "--add", "--index-info", stdin=info, env=env)
        return _git(root, "write-tree", env=env).strip()


def index_tree(root: Path, directory: str) -> str | None:
    """The tree ID of directory as staged in the index, or None if nothing under it is.

    Reads stage-0 entries only, so the caller checks unmerged() first.
    """
    prefix = directory.rstrip("/") + "/"
    out = _git(root, "ls-files", "-s", "-z", "--", prefix)
    lines = []
    for record in filter(None, out.split("\0")):
        meta, path = record.split("\t", 1)
        if meta.endswith(" 0"):
            lines.append(f"{meta}\t{path[len(prefix):]}")
    return _tree(root, lines) if lines else None


def unmerged(root: Path, directory: str) -> list[str]:
    out = _git(root, "ls-files", "-u", "-z", "--", directory.rstrip("/") + "/")
    return sorted({record.split("\t", 1)[1] for record in filter(None, out.split("\0"))})
