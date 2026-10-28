"""cimkit-git status, in throwaway repos."""

import json
import subprocess

import pytest

from cimkit_git import cli, git, source, state
from cimkit_git.formats import zip_json
from tests.test_git import commit, repo, run  # noqa: F401

ATTRS = "**/*.aprx.src/** -text\n"
VALUE = r"DATABASE=C:\x.gdb"


def layer(conn):
    return json.dumps({"dataConnection": {"workspaceConnectionString": conn}}).encode()


ENTRIES = {"GISProject.json": b'{"version":"3.3.0"}', "map/map.json": layer(VALUE)}


@pytest.fixture
def project(repo, monkeypatch):
    (repo / ".gitattributes").write_text(ATTRS)
    monkeypatch.chdir(repo)
    return repo


def status(capsys):
    assert cli.main(["status"]) == 0
    return capsys.readouterr().out.splitlines()


def test_a_binary_without_source_is_a_new_project(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")

    assert status(capsys) == ["map.aprx: new project"]


def write_source(path, entries=ENTRIES):
    source.write_dir(source.render(entries), path)


def test_source_without_a_binary_is_not_built(project, capsys):
    write_source(project / "maps" / "map.aprx.src")

    assert status(capsys) == ["maps/map.aprx: not built"]


def test_both_sides_without_a_record_is_no_record(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")
    write_source(project / "map.aprx.src")

    assert status(capsys) == ["map.aprx: no record"]


def record_sync(repo, name, entries=ENTRIES, neutral=None, target="local", used=None):
    """Write what explode or build leaves behind: both sides and a matching record.

    neutral is the Source side when placeholders replaced values; used maps each value
    the build used to its key.
    """
    files = source.render(neutral or entries)
    binary = repo / name
    zip_json.write(entries, binary)
    source.write_dir(files, binary.with_name(binary.name + ".src"))
    path = repo / ".git" / "cimkit" / "state.json"
    st = state.load(path)
    st.records[name] = state.Record(
        source_tree=git.files_tree(repo, files),
        binary_hash=source.entries_hash(entries),
        target=target,
        mapping={state.hash_value(st.salt, v): k for v, k in (used or {}).items()},
    )
    state.save(st, path)
    return binary


def test_untouched_sides_are_clean(project, capsys):
    record_sync(project, "map.aprx")

    assert status(capsys) == ["map.aprx: clean"]


def test_an_edited_binary_is_binary_edited(project, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write({**ENTRIES, "GISProject.json": b'{"version":"3.4.0"}'}, binary)

    assert status(capsys) == ["map.aprx: binary edited"]


def test_a_binary_rewritten_with_the_same_content_is_clean(project, capsys):
    binary = record_sync(project, "map.aprx")
    # Pro's spacing differs from the codec's, so the entry hash changes but the Source doesn't.
    zip_json.write({**ENTRIES, "GISProject.json": b'{ "version": "3.3.0" }'}, binary)

    assert status(capsys) == ["map.aprx: clean"]


def test_an_edited_source_is_source_changed(project, capsys):
    record_sync(project, "map.aprx")
    (project / "map.aprx.src" / "GISProject.json").write_text('{\n  "version": "3.4.0"\n}\n')

    assert status(capsys) == ["map.aprx: Source changed"]


CONFIG = """
[git.placeholders]
fields  = ["workspaceConnectionString"]
format  = "@@{key}@@"
keys    = ["main_gdb"]
targets = ["local", "dev"]
"""
NEUTRAL = {**ENTRIES, "map/map.json": layer("@@main_gdb@@")}
REFORMATTED = {**ENTRIES, "GISProject.json": b'{ "version": "3.3.0" }'}


@pytest.fixture
def configured(project):
    (project / "cimkit.toml").write_text(CONFIG)
    return project


def test_a_rewritten_binary_explodes_through_the_recorded_values(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[dev]\nmain_gdb = '{VALUE}'\n")
    binary = record_sync(configured, "map.aprx", neutral=NEUTRAL, target="dev", used={VALUE: "main_gdb"})
    zip_json.write(REFORMATTED, binary)

    assert status(capsys) == ["map.aprx: clean"]


def test_a_visible_value_the_recorded_build_did_not_use_is_not_reversed(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[dev]\nmain_gdb = '{VALUE}'\n")
    binary = record_sync(configured, "map.aprx", neutral=NEUTRAL, target="dev", used={"DATABASE=old": "main_gdb"})
    zip_json.write(REFORMATTED, binary)

    assert status(capsys) == ["map.aprx: binary edited"]


def test_without_minus_text_on_source_status_says_git_converts_line_endings(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    record_sync(repo, "map.aprx")

    assert status(capsys)[0] == "map.aprx: git converts line endings in Source"


def test_a_staged_source_edit_that_the_working_copy_dropped_is_a_staged_edit(project, capsys):
    record_sync(project, "map.aprx")
    gis = project / "map.aprx.src" / "GISProject.json"
    original = gis.read_bytes()
    gis.write_text('{\n  "version": "3.4.0"\n}\n')
    run(project, "add", "map.aprx.src")
    gis.write_bytes(original)

    assert status(capsys) == ["map.aprx: staged edit"]


def test_unmerged_source_is_unmerged(project, capsys):
    gis = "map.aprx.src/GISProject.json"
    commit(project, {gis: b"base\n", ".gitattributes": ATTRS.encode()})
    run(project, "switch", "-qc", "side")
    commit(project, {gis: b"side\n"})
    run(project, "switch", "-q", "main")
    commit(project, {gis: b"main\n"})
    subprocess.run(["git", "merge", "-q", "side"], cwd=project, capture_output=True)

    assert status(capsys) == ["map.aprx: unmerged"]


def test_projects_are_found_under_the_config_dir_minus_exclude_and_hidden_dirs(project, monkeypatch, capsys):
    gis = project / "gis"
    gis.mkdir()
    (gis / "cimkit.toml").write_text('[git]\nexclude = ["archive/*"]\n')
    for name in ["outside.aprx", "gis/a.aprx", "gis/sub/b.aprx", "gis/archive/old/c.aprx", "gis/.backup/d.aprx"]:
        (project / name).parent.mkdir(parents=True, exist_ok=True)
        zip_json.write(ENTRIES, project / name)
    monkeypatch.chdir(gis / "sub")

    assert status(capsys) == ["gis/a.aprx: new project", "gis/sub/b.aprx: new project"]


def test_a_project_that_cannot_be_read_is_reported_and_the_rest_still_are(project, capsys):
    (project / "bad.aprx").write_bytes(b"not a zip")
    zip_json.write(ENTRIES, project / "good.aprx")

    assert cli.main(["status"]) == 1
    out = capsys.readouterr().out.splitlines()
    assert out[0].startswith("bad.aprx: error: ")
    assert out[1] == "good.aprx: new project"


def test_outside_a_repo_status_fails_with_a_message(tmp_path, monkeypatch, capsys):
    monkeypatch.chdir(tmp_path)

    assert cli.main(["status"]) == 1
    assert "not inside a git repository" in capsys.readouterr().err


def test_the_line_endings_status_says_which_line_to_add(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    zip_json.write(ENTRIES, repo / "map.aprx")

    cli.main(["status"])

    assert "**/*.aprx.src/** -text" in capsys.readouterr().out
