import json
import zipfile
from pathlib import Path

from cimkit_git.explode import explode


def test_default_output_name(tmp_path, simple_aprx):
    import shutil
    aprx_copy = tmp_path / "simple.aprx"
    shutil.copy(simple_aprx, aprx_copy)
    out = explode(str(aprx_copy))
    assert out.name == "simple.aprx.src"
    assert out.parent == tmp_path


def test_custom_output_path(tmp_path, simple_aprx):
    custom = tmp_path / "my_output"
    out = explode(str(simple_aprx), str(custom))
    assert out == custom
    assert out.is_dir()


def test_all_files_present(tmp_path, simple_aprx):
    out = explode(str(simple_aprx), str(tmp_path / "simple.aprx.src"))
    with zipfile.ZipFile(simple_aprx) as zf:
        expected = set(zf.namelist())
    actual = {f.relative_to(out).as_posix() for f in out.rglob("*") if f.is_file()}
    assert actual == expected


def test_json_is_pretty_printed(exploded):
    text = (exploded / "GISProject.json").read_text()
    assert "\n" in text
    assert "  " in text


def test_xml_is_formatted(exploded):
    text = (exploded / "DocumentInfo.xml").read_text()
    assert "\n" in text


def test_second_explode_replaces_cleanly(tmp_path, simple_aprx):
    out = tmp_path / "simple.aprx.src"
    explode(str(simple_aprx), str(out))
    stale = out / "stale_file.json"
    stale.write_text("{}")
    explode(str(simple_aprx), str(out))
    assert not stale.exists()
