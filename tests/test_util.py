import pytest
from pathlib import Path

from cimkit_git.util import (
    src_dir_for,
    aprx_for_src_dir,
    aprx_output_for,
    is_aprx_src_dir,
)


def test_src_dir_for_bare_name():
    assert src_dir_for(Path("simple.aprx")) == Path("simple.aprx.src")


def test_src_dir_for_preserves_parent():
    assert src_dir_for(Path("/some/path/simple.aprx")) == Path("/some/path/simple.aprx.src")


def test_aprx_for_src_dir():
    assert aprx_for_src_dir(Path("simple.aprx.src")) == Path("simple.aprx")


def test_aprx_for_src_dir_preserves_parent():
    assert aprx_for_src_dir(Path("/some/path/simple.aprx.src")) == Path("/some/path/simple.aprx")


def test_aprx_for_src_dir_raises_on_non_conforming():
    with pytest.raises(ValueError):
        aprx_for_src_dir(Path("simple"))


def test_aprx_for_src_dir_raises_on_plain_aprx():
    with pytest.raises(ValueError):
        aprx_for_src_dir(Path("simple.aprx"))


def test_aprx_output_for_conventional_matches_strict():
    # On a conventional dir, the lenient sibling agrees with aprx_for_src_dir.
    assert aprx_output_for(Path("map.aprx.src")) == Path("map.aprx")


def test_aprx_output_for_strips_trailing_src():
    assert aprx_output_for(Path("data.src")) == Path("data.aprx")


def test_aprx_output_for_bare_dir_appends_aprx():
    assert aprx_output_for(Path("myfolder")) == Path("myfolder.aprx")


def test_aprx_output_for_preserves_parent():
    assert aprx_output_for(Path("/some/path/myfolder")) == Path("/some/path/myfolder.aprx")


def test_aprx_output_for_never_raises_on_non_conforming():
    # Unlike aprx_for_src_dir, the lenient sibling produces a name instead of raising.
    assert aprx_output_for(Path("simple")) == Path("simple.aprx")


def test_is_aprx_src_dir_true(tmp_path):
    d = tmp_path / "simple.aprx.src"
    d.mkdir()
    (d / "GISProject.json").write_text("{}")
    assert is_aprx_src_dir(d) is True


def test_is_aprx_src_dir_false_wrong_name(tmp_path):
    d = tmp_path / "simple"
    d.mkdir()
    (d / "GISProject.json").write_text("{}")
    assert is_aprx_src_dir(d) is False


def test_is_aprx_src_dir_false_missing_gisproject(tmp_path):
    d = tmp_path / "simple.aprx.src"
    d.mkdir()
    assert is_aprx_src_dir(d) is False
