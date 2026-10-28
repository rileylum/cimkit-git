"""Properties of the whole path: binary -> Source -> binary."""

import json
import os
from pathlib import Path

import pytest

from cimkit_git import errors, source
from cimkit_git.formats import zip_json

FIXTURE = Path(__file__).parent / "fixtures" / "simple" / "simple.aprx"
CORPUS = Path(os.environ.get("CIMKIT_CORPUS", Path(__file__).parents[2] / "cimkit-corpus" / "sources"))

# Shaped like a Pro write: compact JSON with long floats, CRLF XML, a Map/map collision.
PRO_ENTRIES = {
    "GISProject.json": b'{"version":"3.3.0","scale":0.18431372549019609,"tiny":0.0000005}',
    "Map/3f1c.json": b'{"type":"CIMMap"}',
    "map/map.json": b'{"name":"Map"}',
    "Metadata/doc.xml": b"<?xml version='1.0'?>\r\n<a xmlns:typens='urn:t'><b> </b><c>x<d/>y</c></a>",
    "Thumbnail.dat": bytes(range(256)) * 4,
}


def explode(path):
    return source.render(zip_json.read(path))


def build(files, path):
    zip_json.write(source.parse(files), path)


def assert_fixed_point(original, tmp_path):
    first = explode(original)
    build(first, tmp_path / "1.aprx")
    second = explode(tmp_path / "1.aprx")
    build(second, tmp_path / "2.aprx")

    assert second == first
    assert (tmp_path / "2.aprx").read_bytes() == (tmp_path / "1.aprx").read_bytes()


def test_explode_then_build_reaches_a_fixed_point_after_one_cycle(tmp_path):
    zip_json.write(PRO_ENTRIES, tmp_path / "pro.aprx")

    assert_fixed_point(tmp_path / "pro.aprx", tmp_path)


def test_opaque_entries_survive_a_build_byte_for_byte(tmp_path):
    zip_json.write(PRO_ENTRIES, tmp_path / "pro.aprx")

    build(explode(tmp_path / "pro.aprx"), tmp_path / "built.aprx")

    assert zip_json.read(tmp_path / "built.aprx")["Thumbnail.dat"] == PRO_ENTRIES["Thumbnail.dat"]


def test_the_pro_written_fixture_reaches_a_fixed_point(tmp_path):
    assert_fixed_point(FIXTURE, tmp_path)


def _corpus_projects():
    if not CORPUS.is_dir():
        return []
    return sorted(p for p in CORPUS.rglob("*.aprx") if p.is_file())


@pytest.mark.skipif(not CORPUS.is_dir(), reason="cimkit-corpus is not present")
@pytest.mark.parametrize("path", _corpus_projects(), ids=lambda p: str(p.relative_to(CORPUS)))
def test_corpus_project_reaches_a_fixed_point(path, tmp_path):
    try:
        assert_fixed_point(path, tmp_path)
    except (errors.NotAZipError, errors.Pro2ProjectError):
        pytest.skip("not a Pro 3.x project")
    original, built = zip_json.read(path), zip_json.read(tmp_path / "1.aprx")
    assert built.keys() == original.keys()
    for name, data in original.items():
        if data.lstrip()[:1] in (b"{", b"["):
            assert json.loads(built[name]) == json.loads(data), name
