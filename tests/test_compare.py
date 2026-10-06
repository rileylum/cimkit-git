import json
import zipfile
from pathlib import Path

from cimkit_git.compare import compare
from cimkit_git.pack import pack


def _make_modified_aprx(original: Path, dest: Path, filename: str, data: bytes) -> Path:
    """Copy original .aprx to dest, replacing one file's content."""
    with zipfile.ZipFile(original, "r") as src, \
         zipfile.ZipFile(dest, "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            dst.writestr(item, data if item.filename == filename else src.read(item.filename))
    return dest


def test_identical_file_no_diff(simple_aprx):
    assert compare(str(simple_aprx), str(simple_aprx)) is False


def test_packed_roundtrip_no_diff(tmp_path, simple_aprx, exploded):
    packed = pack(str(exploded), str(tmp_path / "repacked.aprx"))
    assert compare(str(simple_aprx), str(packed)) is False


def test_changed_json_field_detected(tmp_path, simple_aprx):
    with zipfile.ZipFile(simple_aprx) as zf:
        original = json.loads(zf.read("GISProject.json"))
    original["_test_marker"] = "changed"
    modified_bytes = json.dumps(original, separators=(",", ":")).encode()

    modified = _make_modified_aprx(simple_aprx, tmp_path / "modified.aprx", "GISProject.json", modified_bytes)
    assert compare(str(simple_aprx), str(modified)) is True


def test_diff_output_contains_changed_field(tmp_path, simple_aprx, capsys):
    with zipfile.ZipFile(simple_aprx) as zf:
        original = json.loads(zf.read("GISProject.json"))
    original["_test_marker"] = "changed"
    modified_bytes = json.dumps(original, separators=(",", ":")).encode()

    modified = _make_modified_aprx(simple_aprx, tmp_path / "modified.aprx", "GISProject.json", modified_bytes)
    compare(str(simple_aprx), str(modified))
    assert "_test_marker" in capsys.readouterr().out


def test_missing_file_detected(tmp_path, simple_aprx):
    with zipfile.ZipFile(simple_aprx, "r") as src, \
         zipfile.ZipFile(tmp_path / "missing.aprx", "w", compression=zipfile.ZIP_DEFLATED) as dst:
        for item in src.infolist():
            if item.filename != "Index.json":
                dst.writestr(item, src.read(item.filename))
    assert compare(str(simple_aprx), str(tmp_path / "missing.aprx")) is True


def test_directory_vs_aprx_no_diff(simple_aprx, exploded):
    assert compare(str(exploded), str(simple_aprx)) is False
