"""Thin wrapper over the git commands the sync decisions need."""

import os
import subprocess
import tempfile
from collections.abc import Mapping
from pathlib import Path

from cimkit_git.errors import NotAGitRepoError, RevError, SymlinkInSourceError


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


def ignored(root: Path, path: Path) -> bool:
    """Whether git ignores path. A tracked file counts as not ignored."""
    return subprocess.run(["git", "check-ignore", "-q", str(path)], cwd=root).returncode == 0


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


def head_tree(root: Path, directory: str) -> str | None:
    """The tree ID of directory in HEAD, or None if HEAD doesn't exist or lacks it."""
    try:
        return _git(root, "rev-parse", "--verify", "-q", f"HEAD:{directory.rstrip('/')}").strip()
    except subprocess.CalledProcessError:
        return None


def unmerged(root: Path, directory: str) -> list[str]:
    out = _git(root, "ls-files", "-u", "-z", "--", directory.rstrip("/") + "/")
    return sorted({record.split("\t", 1)[1] for record in filter(None, out.split("\0"))})


def rev_tree(root: Path, rev: str) -> str:
    try:
        return _git(root, "rev-parse", "--verify", "-q", f"{rev}^{{tree}}").strip()
    except subprocess.CalledProcessError as exc:
        raise RevError(f"{rev} names no commit or tree in this repo") from exc


def write_tree(root: Path) -> str:
    """The tree ID of the whole index, written into the object store. Fails on unmerged
    entries, which git refuses to commit before pre-commit runs."""
    return _git(root, "write-tree").strip()


def outgoing(root: Path, shas: list[str], remote: str) -> list[str]:
    """Commits reachable from any of shas that no ref of remote holds, oldest first, once each.

    Compared with the remote-tracking refs rather than the sha git passes for the remote
    side: that sha is all zeros for a new branch, and may be missing here after someone
    else's push.
    """
    return _git(root, "rev-list", "--reverse", *shas, "--not", f"--remotes={remote}").split()


def _ls_tree(root: Path, tree: str, directory: str) -> dict[str, str]:
    """Path relative to directory -> blob ID, for every file under directory in tree."""
    prefix = directory.rstrip("/") + "/" if directory else ""
    out = _git(root, "ls-tree", "-r", "-z", "--full-tree", tree, "--", prefix or ".")
    blobs = {}
    for record in filter(None, out.split("\0")):
        meta, path = record.split("\t", 1)
        mode, kind, oid = meta.split()
        if kind != "blob":
            continue  # a submodule holds no Source
        # A link could pull a file from outside the repo into a check or a build.
        if mode == "120000":
            raise SymlinkInSourceError(f"{path} is a symlink; Source must hold plain files")
        blobs[path[len(prefix):]] = oid
    return blobs


def tree_paths(root: Path, tree: str, directory: str = "") -> list[str]:
    return sorted(_ls_tree(root, tree, directory))


def tree_files(root: Path, tree: str, directory: str) -> dict[str, bytes]:
    """Every file under directory in tree, keyed by its path relative to directory."""
    blobs = _ls_tree(root, tree, directory)
    if not blobs:
        return {}
    out = subprocess.run(
        ["git", "cat-file", "--batch"],
        cwd=root,
        input="".join(oid + "\n" for oid in blobs.values()).encode(),
        capture_output=True,
        check=True,
    ).stdout
    files, pos = {}, 0
    # Each object comes back as "<oid> blob <size>\n<bytes>\n", in request order.
    for path in blobs:
        header_end = out.index(b"\n", pos)
        size = int(out[pos:header_end].split()[2])
        files[path] = out[header_end + 1 : header_end + 1 + size]
        pos = header_end + 1 + size + 1
    return dict(sorted(files.items()))


def show(root: Path, tree: str, path: str) -> bytes | None:
    """The file at path in tree, or None if there is none."""
    try:
        return subprocess.run(
            ["git", "cat-file", "blob", f"{tree}:{path}"], cwd=root, capture_output=True, check=True
        ).stdout
    except subprocess.CalledProcessError:
        return None
