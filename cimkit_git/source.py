"""Entries <-> Source files: name validation, case escaping, rendering."""

import hashlib
import json
import os
from collections.abc import Callable, Mapping
from pathlib import Path
from xml.dom import minidom
from xml.parsers.expat import ExpatError

from cimkit_git.errors import BadEntryNameError, DuplicateEntryError, EntryParseError, SymlinkInSourceError

NAMES_FILE = "_names.json"
XML_DECL = '<?xml version="1.0" encoding="utf-8"?>'


def _kind(data: bytes) -> str:
    # Suffixes lie: Pro 3.x stores JSON in .xml entries, and .atbx in .content and .rc.
    first = data.lstrip()[:1]
    if first in (b"{", b"["):
        return "json"
    if first == b"<":
        return "xml"
    return "opaque"


def _is_blank(node: minidom.Node) -> bool:
    # XML whitespace only: str.isspace() would also accept a no-break space, which is a value.
    return node.nodeType == node.TEXT_NODE and not node.data.strip(" \t\r\n")


def _reformat(node: minidom.Node, indent: str | None, depth: int = 0) -> None:
    """Replace the whitespace between child elements, recursively.

    Only elements whose children are all elements are touched. Text, including
    whitespace-only values such as <x> </x>, and mixed content are values and must
    pass through unchanged. indent=None strips the whitespace, for build.
    """
    children = list(node.childNodes)
    structural = [c for c in children if not _is_blank(c)]
    if structural and all(c.nodeType == c.ELEMENT_NODE for c in structural):
        for child in children:
            if _is_blank(child):
                node.removeChild(child)
        if indent is not None:
            doc = node.ownerDocument
            for child in structural:
                node.insertBefore(doc.createTextNode("\n" + indent * (depth + 1)), child)
            node.appendChild(doc.createTextNode("\n" + indent * depth))
    for child in structural:
        if child.nodeType == child.ELEMENT_NODE:
            _reformat(child, indent, depth + 1)


def _xml(data: bytes, indent: str | None) -> bytes:
    doc = minidom.parseString(data)
    # Documents cannot hold text nodes, so the root's own siblings are joined by hand.
    nodes = list(doc.childNodes)
    for node in nodes:
        _reformat(node, indent)
    sep = "" if indent is None else "\n"
    body = sep.join(node.toxml() for node in nodes)
    return (XML_DECL + sep + body + sep).encode()


def _reformat_entry(data: bytes, pretty: bool) -> bytes:
    kind = _kind(data)
    if kind == "json":
        obj = json.loads(data)
        if pretty:
            return (json.dumps(obj, indent=2, ensure_ascii=False) + "\n").encode()
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode()
    if kind == "xml":
        return _xml(data, "  " if pretty else None)
    return data


def _render_entry(data: bytes) -> bytes:
    return _reformat_entry(data, pretty=True)


def _parse_entry(data: bytes) -> bytes:
    return _reformat_entry(data, pretty=False)


def _convert(items: Mapping[str, bytes], convert: Callable[[bytes], bytes]) -> dict[str, bytes]:
    out = {}
    for name, data in items.items():
        try:
            out[name] = convert(data)
        except (ValueError, ExpatError) as exc:
            # Never fall back to raw bytes: unparsed structure can hide a value from the leak scan.
            raise EntryParseError(f"{name}: {exc}") from exc
    return out


# Rejected on every OS, so a Source written on Linux still checks out on Windows.
_RESERVED = {"CON", "PRN", "AUX", "NUL", *(f"COM{i}" for i in range(1, 10)), *(f"LPT{i}" for i in range(1, 10))}
_ILLEGAL = set('<>:"|?*\\') | {chr(i) for i in range(32)}


def _check_name(name: str) -> None:
    if name == NAMES_FILE:
        raise BadEntryNameError(f"{name}: the name is reserved for the case-escape map")
    for segment in name.split("/"):
        if (
            segment in ("", ".", "..")
            or _ILLEGAL.intersection(segment)
            or segment[-1] in ". "
            or segment.split(".")[0].rstrip(" ").upper() in _RESERVED
        ):
            raise BadEntryNameError(f"{name}: not a safe relative path on every OS")


def _prefixes(name: str) -> list[str]:
    segments = name.split("/")
    return ["/".join(segments[: i + 1]) for i in range(len(segments))]


def _escape(names: list[str]) -> dict[str, str]:
    """Map each original path prefix that collides by case to a unique escaped prefix.

    The prefix that sorts first keeps its name; each later one gets ~N on its last
    segment, with N the lowest number no other path uses. Assignment depends on the
    full name set, so adding a colliding sibling can rename another one.
    """
    real = {p.lower() for name in names for p in _prefixes(name)}
    taken = {NAMES_FILE}
    placed: dict[str, str] = {}  # original prefix -> Source prefix
    escapes: dict[str, str] = {}
    for name in sorted(names):
        for prefix in _prefixes(name):
            if prefix in placed:
                continue
            parent, _, segment = prefix.rpartition("/")
            base = f"{placed[parent]}/{segment}" if parent else segment
            candidate, n = base, 1
            while candidate.lower() in taken or (n > 1 and candidate.lower() in real):
                n += 1
                candidate = f"{base}~{n}"
            if n > 1:
                escapes[prefix] = candidate
            placed[prefix] = candidate
            taken.add(candidate.lower())
    return escapes


def _rename(name: str, mapping: Mapping[str, str]) -> str:
    # Longest prefix first, so a nested escape wins over its parent's.
    for prefix in reversed(_prefixes(name)):
        if prefix in mapping:
            return mapping[prefix] + name[len(prefix) :]
    return name


def render(entries: Mapping[str, bytes]) -> dict[str, bytes]:
    for name in entries:
        _check_name(name)
    files = _convert(entries, _render_entry)
    escapes = _escape(list(files))
    if not escapes:
        return files
    files = {_rename(name, escapes): data for name, data in files.items()}
    restore = {esc: orig for orig, esc in escapes.items()}
    files[NAMES_FILE] = _render_entry(json.dumps(restore, sort_keys=True).encode())
    return files


def _load_restore(data: bytes | None) -> dict[str, str]:
    if data is None:
        return {}
    try:
        restore = json.loads(data)
    except ValueError as exc:
        raise EntryParseError(f"{NAMES_FILE}: {exc}") from exc
    if not isinstance(restore, dict) or not all(isinstance(v, str) for v in restore.values()):
        raise EntryParseError(f"{NAMES_FILE}: expected an object of escaped name to original name")
    return restore


def parse(files: Mapping[str, bytes]) -> dict[str, bytes]:
    files = dict(files)
    restore = _load_restore(files.pop(NAMES_FILE, None))
    entries: dict[str, bytes] = {}
    # Source can come from any commit, so restored names get the same checks as zip names.
    for name, data in _convert(files, _parse_entry).items():
        original = _rename(name, restore)
        _check_name(original)
        if original in entries:
            raise DuplicateEntryError(f"{NAMES_FILE} restores more than one file to {original}")
        entries[original] = data
    return dict(sorted(entries.items()))


def entries_hash(entries: Mapping[str, bytes]) -> str:
    """Hash a binary by its uncompressed entries, never its zip bytes.

    DEFLATE output varies with the zlib build, so hashing the zip would flag a binary
    built on another machine as edited. Changing this encoding invalidates every
    recorded binary_hash.
    """
    h = hashlib.sha256()
    for name in sorted(entries):
        # Length prefixes stop ("a", b"bc") and ("ab", b"c") hashing alike.
        for part in (name.encode(), entries[name]):
            h.update(len(part).to_bytes(8, "big"))
            h.update(part)
    return h.hexdigest()


def read_dir(path: Path) -> dict[str, bytes]:
    files = {}
    for root, dirs, names in os.walk(path):
        for name in dirs + names:
            full = Path(root, name)
            # A link could pull a file from outside the repo into a build.
            if full.is_symlink():
                raise SymlinkInSourceError(f"{full} is a symlink; Source must hold plain files")
        for name in names:
            full = Path(root, name)
            files[full.relative_to(path).as_posix()] = full.read_bytes()
    return dict(sorted(files.items()))


def write_dir(files: Mapping[str, bytes], path: Path) -> None:
    """Write Source files into a new directory.

    The directory must not exist, so a file from an earlier explode can never survive
    into this one. Callers write to a temp path and rename it into place.
    """
    path.mkdir(parents=True)
    for name, data in files.items():
        target = path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
