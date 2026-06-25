"""The one Entry reader (issue 0002): enumeration, the parse-fallback policy, and the
canonical renderings — exercised through the single interface five commands now share."""

import json
import zipfile

import pytest

from aprx_tools.entry import (
    JSON,
    OPAQUE,
    XML,
    normalise,
    parsed_json_entries,
    read_entries,
    render_min,
    render_pretty,
)

# A non-UTF-8 blob — a stand-in for a thumbnail / .dat. If any layer decodes it as text
# the bytes change, so it is the sharp edge of the byte-passthrough guarantee.
THUMBNAIL = bytes(range(256))


def _make_project(root, entries: dict):
    """Write `entries` (name -> bytes) both as a .aprx zip and as a Source dir, so a test
    can assert the reader treats the two transparently."""
    aprx = root / "p.aprx"
    with zipfile.ZipFile(aprx, "w") as zf:
        for name, data in entries.items():
            zf.writestr(name, data)
    src = root / "p.aprx.src"
    for name, data in entries.items():
        target = src / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
    return aprx, src


@pytest.fixture
def project(tmp_path):
    return _make_project(tmp_path, {
        "GISProject.json": json.dumps({"b": 2, "a": 1}).encode(),
        "doc.xml": b"<root><child>x</child></root>",
        "thumbnail.dat": THUMBNAIL,
        "bad.json": b"{not valid json",
    })


# --------------------------------------------------------------------------- #
# read_entries — zip OR dir, transparently; classify + parse once.
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("which", ["aprx", "src"])
def test_reads_zip_and_dir_identically(project, which):
    aprx, src = project
    entries = {e.name: e for e in read_entries(aprx if which == "aprx" else src)}
    assert entries["GISProject.json"].kind == JSON
    assert entries["GISProject.json"].parsed == {"a": 1, "b": 2}
    assert entries["doc.xml"].kind == XML
    assert entries["doc.xml"].parsed is None
    assert entries["thumbnail.dat"].kind == OPAQUE
    assert entries["thumbnail.dat"].raw == THUMBNAIL


def test_entries_are_sorted(project):
    aprx, _ = project
    names = [e.name for e in read_entries(aprx)]
    assert names == sorted(names)


# --------------------------------------------------------------------------- #
# Parse-fallback policy — one place decides what an unparseable entry means.
# --------------------------------------------------------------------------- #

def test_top_level_null_json_is_parsed_not_corrupt(tmp_path):
    """A .json whose content is literally `null` parses to None — a clean parse, not a
    failure. `parsed is None` must NOT be read as 'unparseable' (regression guard)."""
    aprx, _ = _make_project(tmp_path, {"GISProject.json": b"null"})
    entry = next(read_entries(aprx))
    assert entry.kind == JSON
    assert entry.is_parsed_json          # the predicate that matters
    assert entry.parsed is None          # ...even though the value is None
    assert entry.error is None
    # It renders as `null`, round-trips, and never raises in normalise.
    assert render_pretty(entry) == "null\n"
    assert normalise(entry) == "null\n"
    assert render_min(entry) == b"null"


def test_bom_encoded_json_parses(tmp_path):
    """json.loads on bytes auto-detects a UTF-16 BOM — bootstrap's old bytes-path
    tolerance, preserved by parsing from raw rather than a forced utf-8 decode."""
    raw = json.dumps({"a": 1}).encode("utf-16")  # carries a BOM
    aprx, _ = _make_project(tmp_path, {"GISProject.json": raw})
    entry = next(read_entries(aprx))
    assert entry.is_parsed_json
    assert entry.parsed == {"a": 1}


def test_unparseable_json_surfaces_as_none_with_error(project):
    aprx, _ = project
    bad = next(e for e in read_entries(aprx) if e.name == "bad.json")
    assert bad.kind == JSON
    assert bad.parsed is None
    assert bad.error is not None


def test_round_trip_passes_unparseable_json_through_and_warns(project, capsys):
    """explode/pack policy: a corrupt .json is rendered as its raw bytes, with one warning."""
    aprx, _ = project
    bad = next(e for e in read_entries(aprx) if e.name == "bad.json")
    assert render_pretty(bad) == bad.raw
    assert render_min(bad) == bad.raw
    assert "could not parse JSON in bad.json" in capsys.readouterr().err


def test_scan_skips_everything_unparseable(project):
    """verify/bootstrap policy: only parseable JSON; xml, opaque and corrupt json drop."""
    aprx, _ = project
    names = {e.name for e in parsed_json_entries(aprx)}
    assert names == {"GISProject.json"}


# --------------------------------------------------------------------------- #
# Byte-passthrough for opaque entries — tested through the one reader (AC4).
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("render", [render_pretty, render_min])
def test_opaque_entry_survives_render_unchanged(project, render):
    aprx, _ = project
    thumb = next(e for e in read_entries(aprx) if e.name == "thumbnail.dat")
    assert render(thumb) == THUMBNAIL


def test_opaque_round_trips_through_pack(tmp_path):
    """The end-to-end guarantee: an opaque blob explodes and packs back byte-for-byte."""
    from aprx_tools.pack import pack
    aprx, src = _make_project(tmp_path, {
        "GISProject.json": json.dumps({"v": "3.0"}).encode(),
        "thumbnail.dat": THUMBNAIL,
    })
    repacked = pack(str(src), str(tmp_path / "out.aprx"))
    with zipfile.ZipFile(repacked) as zf:
        assert zf.read("thumbnail.dat") == THUMBNAIL


# --------------------------------------------------------------------------- #
# The pretty form has one home — explode writes exactly what compare normalises to.
# --------------------------------------------------------------------------- #

def test_explode_pretty_equals_compare_normalise_for_json(project):
    aprx, _ = project
    good = next(e for e in read_entries(aprx) if e.name == "GISProject.json")
    assert render_pretty(good) == normalise(good)


def test_normalise_opaque_is_lossy_decode_not_a_crash(project):
    aprx, _ = project
    thumb = next(e for e in read_entries(aprx) if e.name == "thumbnail.dat")
    # errors="replace" never raises — a binary blob still diffs as *something*.
    assert normalise(thumb) == THUMBNAIL.decode("utf-8", errors="replace")


def test_normalise_raises_on_unparseable_json_so_compare_reports_it(project):
    aprx, _ = project
    bad = next(e for e in read_entries(aprx) if e.name == "bad.json")
    with pytest.raises(Exception):
        normalise(bad)
