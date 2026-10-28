"""Config discovery and parsing: cimkit.toml [git] or pyproject.toml [tool.cimkit.git]."""

import pytest

from cimkit_git import config, errors

TABLE = """
[git.placeholders]
fields  = ["workspaceConnectionString"]
format  = "@@{key}@@"
keys    = ["main_gdb", "archive_gdb"]
targets = ["local", "dev"]
"""


def test_load_reads_placeholders_from_cimkit_toml(tmp_path):
    path = tmp_path / "cimkit.toml"
    path.write_text(TABLE)

    cfg = config.load_config(path)

    assert cfg.placeholders == config.Placeholders(
        fields=("workspaceConnectionString",),
        format="@@{key}@@",
        keys=("main_gdb", "archive_gdb"),
        targets=("local", "dev"),
    )


def test_load_without_placeholders_table_replaces_nothing(tmp_path):
    path = tmp_path / "cimkit.toml"
    path.write_text("[git]\n")

    assert config.load_config(path).placeholders is None


def test_load_reads_the_tool_table_from_pyproject(tmp_path):
    path = tmp_path / "pyproject.toml"
    path.write_text(TABLE.replace("[git.", "[tool.cimkit.git."))

    assert config.load_config(path).placeholders.keys == ("main_gdb", "archive_gdb")


@pytest.mark.parametrize(
    "old, new",
    [
        ('"main_gdb", "archive_gdb"', '"Main_gdb"'),
        ('"local", "dev"', '"local", "dev-1"'),
        ('"local", "dev"', ""),
        ('"@@{key}@@"', '"@@key@@"'),
        ('"@@{key}@@"', '"{key}{key}"'),
        ('"@@{key}@@"', '"{key}"'),
        ('"main_gdb", "archive_gdb"', '"main_gdb", "main_gdb"'),
        ('["workspaceConnectionString"]', '"workspaceConnectionString"'),
        ('["workspaceConnectionString"]', "[]"),
        ("format  =", "formt  ="),
        ("targets = [", "targets = [[",),
    ],
)
def test_load_rejects_invalid_placeholders(tmp_path, old, new):
    path = tmp_path / "cimkit.toml"
    path.write_text(TABLE.replace(old, new))

    with pytest.raises(errors.ConfigError):
        config.load_config(path)


def test_find_returns_the_nearest_cimkit_toml(tmp_path):
    (tmp_path / "cimkit.toml").write_text("[git]\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "cimkit.toml").write_text("[git]\n")
    (tmp_path / "a" / "b").mkdir()

    assert config.find_config(tmp_path / "a" / "b", stop=tmp_path) == tmp_path / "a" / "cimkit.toml"


def test_find_skips_a_pyproject_without_the_table(tmp_path):
    (tmp_path / "cimkit.toml").write_text("[git]\n")
    (tmp_path / "a").mkdir()
    (tmp_path / "a" / "pyproject.toml").write_text("[tool.ruff]\n")

    assert config.find_config(tmp_path / "a", stop=tmp_path) == tmp_path / "cimkit.toml"


def test_find_prefers_cimkit_toml_over_pyproject_in_one_directory(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.cimkit.git]\n")
    (tmp_path / "cimkit.toml").write_text("[git]\n")

    assert config.find_config(tmp_path, stop=tmp_path) == tmp_path / "cimkit.toml"


def test_find_uses_a_pyproject_with_the_table(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[tool.cimkit.git]\n")

    assert config.find_config(tmp_path, stop=tmp_path) == tmp_path / "pyproject.toml"


def test_find_stops_at_the_stop_directory(tmp_path):
    (tmp_path / "cimkit.toml").write_text("[git]\n")
    (tmp_path / "repo").mkdir()

    assert config.find_config(tmp_path / "repo", stop=tmp_path / "repo") is None


def test_exclude_lists_project_globs_and_defaults_to_none(tmp_path):
    path = tmp_path / "cimkit.toml"
    path.write_text("[git]\n")
    assert config.load_config(path).exclude == ()

    path.write_text('[git]\nexclude = ["archive/*", "scratch.aprx"]\n')
    assert config.load_config(path).exclude == ("archive/*", "scratch.aprx")


@pytest.mark.parametrize("value", ['"archive/*"', "[1]"])
def test_exclude_must_be_a_list_of_strings(tmp_path, value):
    path = tmp_path / "cimkit.toml"
    path.write_text(f"[git]\nexclude = {value}\n")

    with pytest.raises(errors.ConfigError):
        config.load_config(path)
