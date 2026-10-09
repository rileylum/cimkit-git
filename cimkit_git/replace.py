"""Puts a finished output in place of the old one, so a failure leaves the old one whole.

Temp names start with a dot, which keeps them out of find_projects.
"""

import shutil
from collections.abc import Callable, Mapping
from pathlib import Path

from cimkit_git import source
from cimkit_git.errors import WriteError


def _beside(dest: Path, suffix: str) -> Path:
    return dest.with_name(f".{dest.name}{suffix}")


def recover_dir(dest: Path) -> None:
    """Undo a replace_dir that stopped between its two renames, and clear its temp dirs."""
    old = _beside(dest, ".old")
    if old.is_dir() and not dest.exists():
        old.rename(dest)
    for leftover in (old, _beside(dest, ".new")):
        if leftover.is_dir():
            shutil.rmtree(leftover)


def replace_dir(dest: Path, files: Mapping[str, bytes], check: Callable[[], None]) -> None:
    """Write files as the new dest. check runs after the write and before the swap; it
    raises to stop the swap and leave dest as it was.

    os.replace can't swap a non-empty dir on Windows, so the swap is two renames, with
    the old dir moved aside first. recover_dir restores it if the second rename never ran;
    the caller runs it first, so a crashed run's temp dirs are gone.
    """
    new = _beside(dest, ".new")
    try:
        source.write_dir(files, new)
        check()
        if dest.exists():
            dest.rename(_beside(dest, ".old"))
        new.rename(dest)
    except OSError as exc:
        # On Windows a file that Pro or an editor holds open fails the rename.
        raise WriteError(f"could not replace {dest}: {exc.strerror}") from exc
    finally:
        recover_dir(dest)


def replace_file(dest: Path, write: Callable[[Path], object], check: Callable[[], None]) -> None:
    """Like replace_dir, for a file: one os.replace, which is atomic on every platform."""
    new = _beside(dest, ".new")
    try:
        write(new)
        check()
        new.replace(dest)
    except OSError as exc:
        raise WriteError(f"could not replace {dest}: {exc.strerror}") from exc
    finally:
        new.unlink(missing_ok=True)
