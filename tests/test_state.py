"""Sync state: the per-worktree record of what the tool last wrote or read."""

import pytest

from cimkit_git import errors, state
from cimkit_git.state import Record

RECORD = Record(source_tree="t" * 40, binary_hash="b" * 64, target="dev", mapping={"h1": "main_gdb"})


def test_saved_records_load_back_unchanged(tmp_path):
    path = tmp_path / "cimkit" / "state.json"
    st = state.load(path)
    st.records["maps/map.aprx"] = RECORD

    state.save(st, path)

    assert state.load(path) == st


@pytest.mark.parametrize(
    "text",
    [
        "{not json",
        "[]",
        '{"records": {}}',
        '{"salt": "s", "records": {"m.aprx": {"target": "dev"}}}',
        '{"salt": "s", "records": {"m.aprx": "x"}}',
    ],
)
def test_a_damaged_state_file_is_a_typed_error(tmp_path, text):
    path = tmp_path / "state.json"
    path.write_text(text)

    with pytest.raises(errors.StateError):
        state.load(path)


def test_a_value_hash_depends_on_the_salt_and_never_holds_the_value():
    value = "C:\\data\\main.gdb"

    assert state.hash_value("s1", value) == state.hash_value("s1", value)
    assert state.hash_value("s1", value) != state.hash_value("s2", value)
    assert state.hash_value("s1", value) != state.hash_value("s1", "C:\\data\\other.gdb")
    assert "main" not in state.hash_value("s1", value)


def test_each_new_state_file_gets_its_own_salt(tmp_path):
    assert state.load(tmp_path / "a.json").salt != state.load(tmp_path / "b.json").salt


PH_KEYS = ("main_gdb", "archive_gdb")


def make_values(by_target):
    from cimkit_git.values import Values

    return Values(PH_KEYS, "local", by_target)


def test_explode_map_keeps_visible_values_whose_hash_the_record_holds_under_that_key():
    salt = "s"
    record = Record(
        source_tree="t",
        binary_hash="b",
        target="dev",
        mapping={state.hash_value(salt, "D:\\dev.gdb"): "main_gdb", state.hash_value(salt, "D:\\arc.gdb"): "main_gdb"},
    )
    vals = make_values(
        {
            "local": {"main_gdb": "C:\\local.gdb"},
            # D:\arc.gdb is visible under archive_gdb, but the build used it for main_gdb.
            "dev": {"main_gdb": "D:\\dev.gdb", "archive_gdb": "D:\\arc.gdb"},
        }
    )

    assert state.explode_map(salt, record, vals) == {"D:\\dev.gdb": "main_gdb"}


def test_explode_map_without_a_record_uses_every_visible_value():
    vals = make_values({"local": {"main_gdb": "C:\\local.gdb"}, "dev": {"archive_gdb": "D:\\arc.gdb"}})

    assert state.explode_map("s", None, vals) == {"C:\\local.gdb": "main_gdb", "D:\\arc.gdb": "archive_gdb"}
