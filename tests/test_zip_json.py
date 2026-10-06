import zipfile

import pytest

from cimkit_git import errors
from cimkit_git.formats import zip_json


def make_zip(path, entries):
    with zipfile.ZipFile(path, "w") as zf:
        for name, data in entries:
            zf.writestr(name, data)
    return path


def test_read_returns_entries_sorted_by_name(tmp_path):
    path = make_zip(tmp_path / "p.aprx", [("b.json", b"{}"), ("a.json", b"[]")])

    entries = zip_json.read(path)

    assert list(entries.items()) == [("a.json", b"[]"), ("b.json", b"{}")]


def test_read_drops_directory_entries(tmp_path):
    path = make_zip(tmp_path / "t.atbx", [("KvaByPhase.tool/", b""), ("KvaByPhase.tool/tool.content", b"{}")])

    assert list(zip_json.read(path)) == ["KvaByPhase.tool/tool.content"]


def test_read_rejects_a_file_that_is_not_a_zip(tmp_path):
    path = tmp_path / "pointer.aprx"
    path.write_bytes(b"/annex/objects/SHA256E-s1234--abcd.aprx\n")

    with pytest.raises(errors.NotAZipError):
        zip_json.read(path)


def test_read_refuses_a_pro_2_project(tmp_path):
    path = make_zip(tmp_path / "old.aprx", [("GISProject.xml", b"<GISProject/>")])

    with pytest.raises(errors.Pro2ProjectError, match="open and save it in Pro 3"):
        zip_json.read(path)


@pytest.mark.filterwarnings("ignore:Duplicate name")
def test_read_rejects_duplicate_entry_names(tmp_path):
    path = make_zip(tmp_path / "p.aprx", [("a.json", b"{}"), ("a.json", b"[]")])

    with pytest.raises(errors.DuplicateEntryError, match="a.json"):
        zip_json.read(path)


def test_write_produces_a_zip_that_reads_back_to_the_same_entries(tmp_path):
    entries = {"GISProject.json": b'{"a":1}', "Thumbnail.dat": bytes(range(256))}
    path = tmp_path / "p.aprx"

    zip_json.write(entries, path)

    assert zip_json.read(path) == entries


def test_write_is_byte_identical_regardless_of_entry_order(tmp_path):
    a, b = tmp_path / "a.aprx", tmp_path / "b.aprx"

    zip_json.write({"x.json": b"{}", "Map/m.json": b"[]"}, a)
    zip_json.write({"Map/m.json": b"[]", "x.json": b"{}"}, b)

    assert a.read_bytes() == b.read_bytes()


def test_write_stamps_every_entry_with_the_fixed_dos_epoch(tmp_path):
    path = tmp_path / "p.aprx"

    zip_json.write({"a.json": b"{}", "b.json": b"{}"}, path)

    with zipfile.ZipFile(path) as zf:
        assert {info.date_time for info in zf.infolist()} == {(1980, 1, 1, 0, 0, 0)}


def test_write_gives_the_same_bytes_on_windows_and_posix(tmp_path, monkeypatch):
    posix, windows = tmp_path / "posix.aprx", tmp_path / "windows.aprx"
    zip_json.write({"a.json": b"{}"}, posix)

    monkeypatch.setattr("sys.platform", "win32")
    zip_json.write({"a.json": b"{}"}, windows)

    assert posix.read_bytes() == windows.read_bytes()
