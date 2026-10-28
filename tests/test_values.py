"""A target's values from cimkit.local.toml and CIMKIT__<TARGET>__<KEY> variables."""

import pytest

from cimkit_git import errors, values
from cimkit_git.config import Placeholders

PH = Placeholders(
    fields=("workspaceConnectionString",),
    format="@@{key}@@",
    keys=("main_gdb", "archive_gdb"),
    targets=("local", "dev"),
)


def load(tmp_path, local="", environ=None):
    path = tmp_path / "cimkit.local.toml"
    path.write_text(local)
    return values.load_values(PH, path, environ or {})


def test_target_values_come_from_the_local_file(tmp_path):
    vals = load(tmp_path, '[local]\nmain_gdb = "C:\\\\data\\\\main.gdb"\n')

    assert vals.for_target("local") == {"main_gdb": "C:\\data\\main.gdb"}


def test_an_environment_variable_overrides_the_local_file(tmp_path):
    vals = load(
        tmp_path,
        '[dev]\nmain_gdb = "file"\narchive_gdb = "kept"\n',
        {"CIMKIT__DEV__MAIN_GDB": "env", "CIMKIT__LOCAL__ARCHIVE_GDB": "a"},
    )

    assert vals.for_target("dev") == {"main_gdb": "env", "archive_gdb": "kept"}
    assert vals.for_target("local") == {"archive_gdb": "a"}


def test_a_missing_local_file_leaves_only_environment_variables(tmp_path):
    vals = values.load_values(PH, tmp_path / "absent.toml", {"CIMKIT__DEV__MAIN_GDB": "env"})

    assert vals.for_target("dev") == {"main_gdb": "env"}


def test_variables_for_undeclared_names_are_ignored(tmp_path):
    vals = load(tmp_path, environ={"CIMKIT__PRD__MAIN_GDB": "x", "CIMKIT__DEV__OTHER": "y", "PATH": "/bin"})

    assert vals.for_target("dev") == {}


@pytest.mark.parametrize(
    "local",
    [
        '[prd]\nmain_gdb = "x"\n',
        '[dev]\nmain_gbd = "x"\n',
        "[dev]\nmain_gdb = 1\n",
        'dev = "x"\n',
        'default_target = "prd"\n',
        "[dev\n",
    ],
)
def test_an_invalid_local_file_is_rejected(tmp_path, local):
    with pytest.raises(errors.ValuesError):
        load(tmp_path, local)


def test_the_default_target_is_the_first_declared_one(tmp_path):
    assert load(tmp_path).default_target == "local"


def test_the_local_file_can_override_the_default_target(tmp_path):
    assert load(tmp_path, 'default_target = "dev"\n').default_target == "dev"


def test_missing_lists_declared_keys_a_target_lacks(tmp_path):
    vals = load(tmp_path, '[dev]\narchive_gdb = "x"\n')

    assert vals.missing("dev") == ["main_gdb"]
    assert vals.missing("local") == ["main_gdb", "archive_gdb"]


def test_an_undeclared_target_is_rejected(tmp_path):
    vals = load(tmp_path)

    with pytest.raises(errors.ValuesError):
        vals.for_target("prd")
    with pytest.raises(errors.ValuesError):
        vals.missing("prd")


def test_value_to_key_maps_every_visible_value_across_targets(tmp_path):
    vals = load(tmp_path, '[local]\nmain_gdb = "a"\n[dev]\nmain_gdb = "b"\narchive_gdb = "c"\n')

    assert vals.value_to_key() == {"a": "main_gdb", "b": "main_gdb", "c": "archive_gdb"}


def test_one_value_under_two_keys_is_ambiguous(tmp_path):
    vals = load(tmp_path, '[local]\nmain_gdb = "same"\n[dev]\narchive_gdb = "same"\n')

    with pytest.raises(errors.AmbiguousValueError):
        vals.value_to_key()
