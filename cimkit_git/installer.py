"""cimkit-git install: point git's hooks at cimkit-git hook. install.py is the old design's."""

import shlex
from pathlib import Path

from cimkit_git import git

HOOKS = ["pre-commit", "pre-push", "post-checkout", "post-merge", "post-rewrite", "post-stash"]

# The first git that reads hook.<name>.command and hook.<name>.event.
CONFIG_HOOKS = (2, 54)

MARKER = "# managed-by: cimkit-git"

# Source must reach the object store byte for byte, or its recorded tree IDs stop matching.
NO_CONVERT = "**/*.aprx.src/** -text"


def install(root: Path, python: str) -> list[tuple[Path, str]]:
    """Register every hook. Returns each hook file install left alone because someone
    else wrote it, with the command that file must run to call cimkit-git."""
    config = git.version() >= CONFIG_HOOKS
    hooks = git.hooks_dir(root)
    add_lines(root / ".gitignore", ["*.aprx", "cimkit.local.toml"])
    add_lines(root / ".gitattributes", [NO_CONVERT])
    left = []
    for event in HOOKS:
        # The absolute Python, because git's sh on Windows won't find Pro's conda env on PATH.
        # cimkit_git.cli, not cimkit_git: the package's __main__ is the old git cim command.
        command = f"{shlex.quote(python)} -m cimkit_git.cli hook {event}"
        if config:
            # A friendly name equal to an event name is a fatal error in git.
            git.register_hook(root, f"cimkit-git-{event}", event, command)
        elif ours(hooks / event):
            write_shim(hooks / event, command)
        else:
            left.append((hooks / event, f'{command} "$@"'))
    return left


def ours(path: Path) -> bool:
    return not path.exists() or MARKER.encode() in path.read_bytes()


def add_lines(path: Path, lines: list[str]) -> None:
    """Append each line path lacks, so install can run again and a user's edits stay.

    Bytes, not text: text mode would rewrite every line ending on Windows.
    """
    data = path.read_bytes() if path.exists() else b""
    missing = [line for line in lines if line.encode() not in data.splitlines()]
    if missing:
        if data and not data.endswith(b"\n"):
            data += b"\n"
        path.write_bytes(data + "".join(line + "\n" for line in missing).encode())


def write_shim(path: Path, command: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    # LF only: git's sh on Windows reads a CR as part of the last argument.
    path.write_bytes(f'#!/bin/sh\n{MARKER}\nexec {command} "$@"\n'.encode())
    path.chmod(0o755)
