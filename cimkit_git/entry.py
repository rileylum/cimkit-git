"""The one home for reading a Project's entries — binary or Source — and rendering them.

A Project ships as an ``.aprx`` (a zip of JSON/XML entries) and is committed as a
Source directory (the exploded, diffable rendering). Five commands used to each open
the zip *or* walk the Source dir, ``json.loads`` every ``.json``, and fall back to raw
bytes when an entry won't parse — the parse-fallback policy CLAUDE.md calls "the main
silent-failure risk", copy-pasted five times and already drifted. This module owns it
once.

``read_entries`` enumerates a Project — zip OR dir, transparently — as parsed
:class:`Entry` objects in sorted name order. Parsing happens here, exactly once; an
``.json`` that will not parse surfaces as ``not entry.is_parsed_json`` carrying
``entry.error``, and **each caller chooses what that means**:

  * Round-trips (explode, pack) want **passthrough** — render the raw bytes unchanged so
    a corrupt or non-JSON entry survives the round-trip rather than being dropped. They
    map :func:`render_pretty` / :func:`render_min` over every entry; those helpers warn
    once and fall back to the raw bytes.
  * Read-only scans (verify, bootstrap) want **skip** — they only care about entries they
    can inspect, so they iterate :func:`parsed_json_entries`, which silently drops
    anything that is not parseable JSON.

That passthrough-vs-skip split is the deliberate, named choice (issue 0002): the *parse*
lives in one place; the two policies are two entry points over it, not five private
copies.

The canonical renderings live here too, so explode and compare can never disagree about
what "pretty" means:

  * :func:`render_pretty` — explode's pretty form (JSON ``indent=2``, formatted XML).
  * :func:`render_min`   — pack's minified form (JSON ``separators=(",",":")``).
  * :func:`normalise`    — compare's semantic-diff form (the same pretty JSON, plus a
    whitespace-stripped XML and a lossy ``errors="replace"`` decode for opaque entries).

The JSON pretty form is the single helper :func:`_pretty_json`, shared by
:func:`render_pretty` and :func:`normalise` — so "what explode writes" and "what compare
normalises to" are the same bytes by construction.

This is a **read + parse + render** abstraction (ADR-0002: it sits *under*
``transform.apply``; explode/pack still hand each parsed entry to the injected
transform). pack's deterministic *write* — sorted order, fixed DOS-epoch timestamp,
``compresslevel=6`` — is a write concern and stays in ``pack.py``; the only piece that
moves here is the sorted *enumeration*. Do not pull the zip writer into this module.
"""

from __future__ import annotations

import json
import sys
import zipfile
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Iterator, Optional

#: The three kinds an entry can have, decided by filename suffix. ``json`` and ``xml``
#: are renderable; ``opaque`` (thumbnails, ``.dat`` blobs, anything else) is only ever
#: copied as raw bytes — corrupting it is the silent-failure risk this module guards.
JSON = "json"
XML = "xml"
OPAQUE = "opaque"


@dataclass
class Entry:
    """One entry of a Project, as read from its zip binary or its Source directory.

    ``parsed`` is the decoded JSON value for a ``json`` entry that parsed cleanly. It is
    ``None`` for every ``xml``/``opaque`` entry (which this module does not parse into
    Python) *and* for a ``json`` entry that failed — and, legitimately, for a ``json``
    entry whose content is the literal ``null``. So ``parsed is None`` is **not** the
    "did it parse?" test: use :attr:`is_parsed_json`, which keys off ``error`` instead, or
    a parseable ``null`` entry would be mistaken for a parse failure. ``error`` holds the
    exception when (and only when) parsing failed. ``raw`` is always the original bytes,
    so passthrough never loses data.
    """

    name: str
    raw: bytes
    kind: str
    parsed: object = None
    error: Optional[Exception] = None

    @property
    def is_parsed_json(self) -> bool:
        """True iff this is a JSON entry that parsed — the predicate every consumer shares
        for "do I have a JSON value to render / transform / scan?". Keyed on ``error``, not
        ``parsed is None``, so a JSON entry that legitimately parsed to ``null`` counts as
        parsed (and renders as ``null``) rather than being treated as corrupt."""
        return self.kind == JSON and self.error is None


def _classify(name: str) -> str:
    if name.endswith(".json"):
        return JSON
    if name.endswith(".xml"):
        return XML
    return OPAQUE


def _make_entry(name: str, raw: bytes) -> Entry:
    """Classify and parse one entry. JSON is decoded here — once — so no caller hand-rolls
    the ``json.loads`` + byte-fallback dance again. Parsing goes straight from *bytes*
    (``json.loads`` auto-detects a UTF-8/16/32 BOM), preserving the encoding tolerance
    bootstrap's old bytes-path had. A parse failure is recorded on the Entry (``error``
    set), not raised: the reader stays policy-free and each caller decides whether that
    means passthrough or skip."""
    kind = _classify(name)
    if kind == JSON:
        try:
            return Entry(name, raw, kind, parsed=json.loads(raw))
        except (json.JSONDecodeError, UnicodeDecodeError) as e:
            return Entry(name, raw, kind, error=e)
    return Entry(name, raw, kind)


def _iter_raw(project_path):
    """Yield ``(name, read)`` for every entry — a ``.aprx`` zip *or* a Source directory —
    in sorted POSIX-path-parts order, where ``read`` is a zero-arg callable returning the
    entry's bytes.

    Reading is deferred so a name-only consumer (:func:`parsed_json_entries`) can skip the
    bytes of opaque blobs it would only discard, rather than pulling every thumbnail into
    memory on each verify / pre-push. Both sources sort by the same path-parts key, so a
    zip and the Source dir it round-trips to enumerate in one identical order — and pack's
    archive stays byte-stable, because that is the order pack always wrote in (sorting
    ``Path`` objects, which is exactly path-parts order)."""
    path = Path(project_path)
    if path.is_dir():
        files = sorted((p for p in path.rglob("*") if p.is_file()),
                       key=lambda p: p.relative_to(path).parts)
        for f in files:
            yield f.relative_to(path).as_posix(), f.read_bytes
    else:
        with zipfile.ZipFile(path, "r") as zf:
            for name in sorted(zf.namelist(), key=lambda n: PurePosixPath(n).parts):
                yield name, (lambda n=name: zf.read(n))


def read_entries(project_path) -> Iterator[Entry]:
    """Yield every entry of a Project — a ``.aprx`` zip *or* a Source directory — as a
    parsed :class:`Entry`, in sorted path-parts order.

    The sorted order is what gives pack its deterministic, byte-stable archive for free
    (the writer in ``pack.py`` simply preserves it). A directory entry's ``name`` is its
    POSIX path relative to the directory root, matching the zip-entry names it round-trips
    to.
    """
    for name, read in _iter_raw(project_path):
        yield _make_entry(name, read())


def parsed_json_entries(project_path) -> Iterator[Entry]:
    """The **skip** policy: yield only entries that are parseable JSON.

    Read-only scans (verify's token scan, bootstrap's value collection) inspect the parsed
    value and have nothing to do with XML, opaque blobs, or a corrupt ``.json`` — so they
    iterate this filtered view and stay silent about what it drops, exactly as the five
    hand-written ``continue``-on-error loops used to (issue 0002). Non-JSON names are
    filtered *before* their bytes are read, so a scan never pays to load a binary blob it
    will discard."""
    for name, read in _iter_raw(project_path):
        if _classify(name) != JSON:
            continue
        entry = _make_entry(name, read())
        if entry.is_parsed_json:
            yield entry


def _pretty_json(parsed) -> str:
    """The single definition of the JSON pretty form — explode writes it and compare
    normalises to it, so a semantic diff can never disagree with what explode produced."""
    return json.dumps(parsed, indent=2, ensure_ascii=False) + "\n"


def _format_xml(raw: bytes) -> str:
    """explode's XML pretty form: indent in place, preserving namespaces."""
    ET.register_namespace("xsi", "http://www.w3.org/2001/XMLSchema-instance")
    ET.register_namespace("xs", "http://www.w3.org/2001/XMLSchema")
    ET.register_namespace("typens", "http://www.esri.com/schemas/ArcGIS/3.6.0")
    root = ET.fromstring(raw.decode("utf-8"))
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def _normalise_xml(raw: bytes) -> str:
    """compare's XML form: drop whitespace-only text/tails before indenting, so two XML
    entries that differ only in incidental whitespace normalise to the same string."""
    def _strip(elem):
        if elem.text and not elem.text.strip():
            elem.text = None
        if elem.tail and not elem.tail.strip():
            elem.tail = None
        for child in elem:
            _strip(child)

    root = ET.fromstring(raw.decode("utf-8"))
    _strip(root)
    ET.indent(root, space="  ")
    return ET.tostring(root, encoding="unicode") + "\n"


def _warn_unparsed(entry: Entry) -> None:
    """The one parse-failure warning for the **passthrough** policy. explode and pack
    both call this before falling back to raw bytes, so a corrupt JSON entry is reported
    once, consistently, instead of via three slightly different copied messages."""
    print(f"  warning: could not parse JSON in {entry.name}: {entry.error}", file=sys.stderr)


def render_pretty(entry: Entry):
    """explode's payload for an entry: the pretty form, or the raw bytes on passthrough.

    JSON renders from ``entry.parsed`` (which the injected transform may have rewritten in
    place) via the shared :func:`_pretty_json`; XML is indented; an opaque entry is copied
    verbatim. An unparseable JSON entry warns once and falls back to its raw bytes — the
    passthrough policy. Returns ``str`` for a rendered entry, ``bytes`` for passthrough."""
    if entry.kind == JSON:
        if not entry.is_parsed_json:
            _warn_unparsed(entry)
            return entry.raw
        return _pretty_json(entry.parsed)
    if entry.kind == XML:
        try:
            return _format_xml(entry.raw)
        except Exception as e:  # malformed XML — pass the bytes through rather than abort
            print(f"  warning: could not parse XML in {entry.name}: {e}", file=sys.stderr)
            return entry.raw
    return entry.raw


def render_min(entry: Entry) -> bytes:
    """pack's payload for an entry: minified JSON, or the raw bytes for everything else.

    JSON renders from ``entry.parsed`` (post-transform) with the most compact separators;
    XML and opaque entries are copied verbatim (pack does not re-minify XML). An
    unparseable JSON entry warns once and falls back to its raw bytes — the passthrough
    policy, identical to explode's."""
    if entry.kind == JSON:
        if not entry.is_parsed_json:
            _warn_unparsed(entry)
            return entry.raw
        return json.dumps(entry.parsed, separators=(",", ":")).encode("utf-8")
    return entry.raw


def normalise(entry: Entry) -> str:
    """compare's semantic-diff form for an entry.

    JSON collapses to the shared pretty form (:func:`_pretty_json`); XML to the
    whitespace-stripped form; an opaque entry to a lossy ``errors="replace"`` decode so a
    binary blob still diffs as *something* rather than exploding. A ``.json`` that will not
    parse raises — compare catches it, reports the entry, and counts it as a difference,
    preserving the old behaviour where an unparseable entry is a diff, not a silent match.
    """
    if entry.kind == JSON:
        if not entry.is_parsed_json:
            raise entry.error
        return _pretty_json(entry.parsed)
    if entry.kind == XML:
        return _normalise_xml(entry.raw)
    return entry.raw.decode("utf-8", errors="replace")
