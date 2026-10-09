"""Registering a new value: the key suggestion and the two file edits."""

import tomllib

import pytest

from cimkit_git.errors import RegisterError
from cimkit_git.register import add_key, set_value, suggest_key


def test_a_gdb_path_suggests_its_stem_with_a_gdb_suffix():
    assert suggest_key(r"DATABASE=C:\data\Main Sales.gdb", ()) == "main_sales_gdb"


def test_a_database_name_suggests_itself():
    assert suggest_key("SERVER=db01;INSTANCE=sde:sqlserver:db01;DATABASE=Sales-2024;USER=gis", ()) == "sales_2024"


def test_a_taken_key_gets_the_first_free_number():
    assert suggest_key("DATABASE=sales", ("sales", "sales_2")) == "sales_3"


def test_a_value_naming_no_gdb_or_database_suggests_nothing():
    assert suggest_key("SERVER=db01;USER=gis", ()) is None



CIMKIT_TOML = """# shared settings
[git.placeholders]
fields  = ["workspaceConnectionString"]  # Pro's name
format  = "@@{key}@@"
keys    = ["main_gdb"]
targets = ["local", "dev"]
"""


def test_add_key_appends_to_keys_and_keeps_the_rest_of_the_file(tmp_path):
    path = tmp_path / "cimkit.toml"
    path.write_text(CIMKIT_TOML)

    add_key(path, "sales")

    assert path.read_text() == CIMKIT_TOML.replace('["main_gdb"]', '["main_gdb", "sales"]')


PYPROJECT = """[tool.other]
keys = ["unrelated"]

[tool.cimkit.git.placeholders]
fields = ["workspaceConnectionString"]
format = "@@{key}@@"
keys = [
    "main_gdb",
]
targets = ["local"]
"""


def test_add_key_edits_only_the_cimkit_tables_keys_in_pyproject(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(PYPROJECT)

    add_key(path, "sales")

    assert path.read_text() == PYPROJECT.replace('"main_gdb",\n', '"main_gdb", "sales",\n')


def test_add_key_refuses_a_keys_array_it_cannot_edit_and_leaves_the_file_alone(tmp_path):
    path = tmp_path / "cimkit.toml"
    text = CIMKIT_TOML.replace('["main_gdb"]', '["main_gdb"  # the main one\n]')
    path.write_text(text)

    with pytest.raises(RegisterError):
        add_key(path, "sales")

    assert path.read_text() == text


# Backslashes and quotes: a connection string needs both escaped in TOML.
WINDOWS = r'DATABASE=C:\data\"new"\main.gdb'


def test_set_value_creates_the_local_file_with_the_targets_table(tmp_path):
    path = tmp_path / "cimkit.local.toml"

    set_value(path, "dev", "sales", WINDOWS)

    assert tomllib.loads(path.read_text()) == {"dev": {"sales": WINDOWS}}


def test_set_value_adds_to_an_existing_table_and_keeps_the_others(tmp_path):
    path = tmp_path / "cimkit.local.toml"
    path.write_text('default_target = "dev"\n\n[dev]  # mine\nmain_gdb = "a"\n\n[prd]\nmain_gdb = "b"\n')

    set_value(path, "dev", "sales", WINDOWS)

    assert tomllib.loads(path.read_text()) == {
        "default_target": "dev",
        "dev": {"main_gdb": "a", "sales": WINDOWS},
        "prd": {"main_gdb": "b"},
    }
    assert path.read_text().startswith('default_target = "dev"\n\n[dev]  # mine\n')


def test_set_value_refuses_a_file_it_cannot_edit_and_leaves_it_alone(tmp_path):
    path = tmp_path / "cimkit.local.toml"
    text = 'dev.main_gdb = "a"\n'
    path.write_text(text)

    with pytest.raises(RegisterError):
        set_value(path, "dev", "sales", WINDOWS)

    assert path.read_text() == text
