"""Codec for zip-of-JSON project files: .aprx (Pro 3.x) and .atbx."""

import zipfile
from collections.abc import Mapping
from pathlib import Path

from cimkit_git.errors import DuplicateEntryError, NotAZipError, Pro2ProjectError


def read(path: Path) -> dict[str, bytes]:
    try:
        zf = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise NotAZipError(f"{path} is not a zip file") from exc
    entries = {}
    with zf:
        for info in zf.infolist():
            # Directory entries carry no data; Source recreates directories from file paths.
            if info.is_dir():
                continue
            if info.filename in entries:
                raise DuplicateEntryError(f"{path} has more than one entry named {info.filename}")
            entries[info.filename] = zf.read(info)
    if "GISProject.xml" in entries:
        raise Pro2ProjectError(f"{path} is a Pro 2.x project; open and save it in Pro 3 first")
    return dict(sorted(entries.items()))


def write(entries: Mapping[str, bytes], path: Path) -> None:
    """Write entries so the same input gives the same zip on every run.

    Sorted order, the fixed timestamp and the pinned level are what make a build
    reproducible; dropping any of them makes every build look like an edit. Pro writes
    the invalid dates (1980, 0, 0) and (1980, 1, 0), so the epoch is the first valid one.
    """
    with zipfile.ZipFile(path, "w") as zf:
        for name in sorted(entries):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            # ZipInfo records the host OS, which would make a Windows build differ.
            info.create_system = 0
            zf.writestr(info, entries[name], compresslevel=6)
