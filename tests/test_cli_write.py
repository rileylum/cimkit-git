"""cimkit-git explode, build and sync, in throwaway repos."""

import json
from pathlib import Path

import pytest

from cimkit_git import cli, source, state
from cimkit_git.formats import zip_json
from tests.test_cli_status import (  # noqa: F401
    ENTRIES,
    NEUTRAL,
    REFORMATTED,
    VALUE,
    layer,
    configured,
    project,
    record_sync,
    status,
)
from tests.test_git import repo, run  # noqa: F401


def test_explode_writes_source_and_leaves_the_project_clean(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")

    assert cli.main(["explode"]) == 0

    assert source.read_dir(project / "map.aprx.src") == source.render(ENTRIES)
    capsys.readouterr()
    assert status(capsys) == ["map.aprx: clean"]


def test_explode_replaces_source_so_a_dropped_entry_does_not_survive(project, capsys):
    binary = record_sync(project, "map.aprx")
    edited = {"GISProject.json": b'{"version":"3.4.0"}'}
    zip_json.write(edited, binary)

    assert cli.main(["explode"]) == 0

    assert source.read_dir(project / "map.aprx.src") == source.render(edited)
    capsys.readouterr()
    assert status(capsys) == ["map.aprx: clean"]


EDITED_GIS = '{\n  "version": "3.4.0"\n}\n'


def edit_source(project):
    (project / "map.aprx.src" / "GISProject.json").write_text(EDITED_GIS)


def test_explode_refuses_when_source_changed_and_leaves_it_alone(project, capsys):
    record_sync(project, "map.aprx")
    edit_source(project)

    assert cli.main(["explode"]) == 1

    assert "map.aprx: refused: Source changed" in capsys.readouterr().out
    assert (project / "map.aprx.src" / "GISProject.json").read_text() == EDITED_GIS


def test_explode_force_overwrites_changed_source(project, capsys):
    record_sync(project, "map.aprx")
    edit_source(project)

    assert cli.main(["explode", "--force"]) == 0

    assert source.read_dir(project / "map.aprx.src") == source.render(ENTRIES)


def test_explode_neutralises_values_and_records_the_default_target(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[local]\nmain_gdb = '{VALUE}'\n")
    zip_json.write(ENTRIES, configured / "map.aprx")

    assert cli.main(["explode"]) == 0

    assert source.read_dir(configured / "map.aprx.src") == source.render(NEUTRAL)
    assert capsys.readouterr().out == "map.aprx: exploded, target local\n"
    # Clean after Pro rewrites the binary only if the record holds the value explode used.
    zip_json.write(REFORMATTED, configured / "map.aprx")
    assert status(capsys) == ["map.aprx: clean"]


def test_explode_target_overrides_the_recorded_target(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[dev]\nmain_gdb = '{VALUE}'\n")
    zip_json.write(ENTRIES, configured / "map.aprx")

    assert cli.main(["explode", "--target", "dev"]) == 0

    assert capsys.readouterr().out == "map.aprx: exploded, target dev\n"


def test_explode_refuses_an_unregistered_value_and_shows_it_with_the_password_masked(configured, capsys):
    secret = "SERVER=db;USER=gis;ENCRYPTED_PASSWORD=00022e68;PASSWORD=hunter2;DATABASE=x"
    zip_json.write({**ENTRIES, "map/map.json": layer(secret)}, configured / "map.aprx")

    assert cli.main(["explode"]) == 1

    out = capsys.readouterr().out
    assert "map/map.json: unregistered value SERVER=db;USER=gis;ENCRYPTED_PASSWORD=***;PASSWORD=***;DATABASE=x" in out
    assert "hunter2" not in out and "00022e68" not in out
    assert "cimkit.local.toml" in out
    assert not (configured / "map.aprx.src").exists()


def test_build_resolves_placeholders_for_the_default_target_and_leaves_the_project_clean(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[local]\nmain_gdb = '{VALUE}'\n")
    source.write_dir(source.render(NEUTRAL), configured / "map.aprx.src")

    assert cli.main(["build"]) == 0

    built = zip_json.read(configured / "map.aprx")
    assert json.loads(built["map/map.json"]) == json.loads(layer(VALUE))
    assert capsys.readouterr().out == "map.aprx: built, target local\n"
    assert status(capsys) == ["map.aprx: clean"]


EDITED = {**ENTRIES, "GISProject.json": b'{"version":"3.4.0"}'}


def test_build_refuses_when_the_binary_was_edited_and_leaves_it_alone(project, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write(EDITED, binary)

    assert cli.main(["build"]) == 1

    assert "map.aprx: refused: binary edited" in capsys.readouterr().out
    assert zip_json.read(binary) == EDITED


def test_build_discard_overwrites_an_edited_binary(project, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write(EDITED, binary)

    assert cli.main(["build", "--discard"]) == 0

    assert zip_json.read(binary) == source.parse(source.render(ENTRIES))


def test_a_project_argument_names_one_project_by_its_binary_or_its_source(project, capsys):
    for name in ["a.aprx", "b.aprx"]:
        zip_json.write(ENTRIES, project / name)

    assert cli.main(["explode", "b.aprx"]) == 0
    assert cli.main(["explode", "b.aprx.src"]) == 0

    assert capsys.readouterr().out == "b.aprx: exploded\n" * 2
    assert not (project / "a.aprx.src").exists()


def test_a_project_argument_that_names_no_project_is_an_error(project, capsys):
    assert cli.main(["explode", "nope.aprx"]) == 1

    assert "nope.aprx is not a project" in capsys.readouterr().err


PRD = r"DATABASE=\\prd\x.gdb"


def test_build_output_writes_an_artifact_and_leaves_the_working_binary_and_record_alone(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[local]\nmain_gdb = '{VALUE}'\n[dev]\nmain_gdb = '{PRD}'\n")
    binary = record_sync(configured, "map.aprx", neutral=NEUTRAL, used={VALUE: "main_gdb"})
    before = binary.read_bytes()

    assert cli.main(["build", "map.aprx", "--target", "dev", "-o", "../out.aprx"]) == 0

    built = zip_json.read(configured.parent / "out.aprx")
    assert json.loads(built["map/map.json"]) == json.loads(layer(PRD))
    assert binary.read_bytes() == before
    capsys.readouterr()
    # A rewritten binary is still clean only while the record holds the local build.
    zip_json.write(REFORMATTED, binary)
    assert status(capsys) == ["map.aprx: clean"]


def test_build_output_needs_a_project(configured, capsys):
    with pytest.raises(SystemExit):
        cli.main(["build", "-o", "out.aprx"])

    assert "-o needs a project" in capsys.readouterr().err


def test_sync_does_each_projects_one_action_and_a_refusal_does_not_stop_the_rest(project, capsys):
    for name in ["a.aprx", "b.aprx", "c.aprx", "d.aprx"]:
        record_sync(project, name)
    for name in ["a", "d"]:
        zip_json.write(EDITED, project / f"{name}.aprx")
    for name in ["b", "d"]:
        (project / f"{name}.aprx.src" / "GISProject.json").write_text(EDITED_GIS)

    assert cli.main(["sync"]) == 1

    assert capsys.readouterr().out.splitlines() == [
        "a.aprx: exploded, target local",
        "b.aprx: built, target local",
        "c.aprx: clean",
        "d.aprx: refused: conflict",
        "    Run explode --force to keep the binary, or build --discard to keep Source.",
    ]
    assert source.read_dir(project / "a.aprx.src") == source.render(EDITED)
    assert json.loads(zip_json.read(project / "b.aprx")["GISProject.json"]) == {"version": "3.4.0"}
    assert status(capsys) == ["a.aprx: clean", "b.aprx: clean", "c.aprx: clean", "d.aprx: conflict"]


def test_sync_builds_a_source_change_for_the_recorded_target(configured, capsys):
    (configured / "cimkit.local.toml").write_text(f"[local]\nmain_gdb = '{VALUE}'\n[dev]\nmain_gdb = '{PRD}'\n")
    record_sync(configured, "map.aprx", entries={**ENTRIES, "map/map.json": layer(PRD)}, neutral=NEUTRAL, target="dev", used={PRD: "main_gdb"})
    edit_source(configured)

    assert cli.main(["sync"]) == 0

    assert capsys.readouterr().out == "map.aprx: built, target dev\n"
    assert json.loads(zip_json.read(configured / "map.aprx")["map/map.json"]) == json.loads(layer(PRD))


def test_a_write_fails_at_once_while_another_run_holds_the_lock_and_status_still_works(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")

    with state.lock(project / ".git" / "cimkit" / "lock"):
        assert cli.main(["sync"]) == 1
        assert "another cimkit-git is running" in capsys.readouterr().err
        assert status(capsys) == ["map.aprx: new project"]

    assert not (project / "map.aprx.src").exists()


def test_explode_refuses_when_source_changes_while_it_writes(project, monkeypatch, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write(EDITED, binary)
    write_dir = source.write_dir

    def write_then_edit(files, path):
        write_dir(files, path)
        edit_source(project)

    monkeypatch.setattr(source, "write_dir", write_then_edit)

    assert cli.main(["explode"]) == 1

    assert "map.aprx: refused: Source changed" in capsys.readouterr().out
    assert (project / "map.aprx.src" / "GISProject.json").read_text() == EDITED_GIS
    assert status(capsys) == ["map.aprx: conflict"]


def test_build_refuses_when_the_binary_changes_while_it_writes(project, monkeypatch, capsys):
    binary = record_sync(project, "map.aprx")
    edit_source(project)
    write = zip_json.write

    def write_then_edit(entries, path):
        write(entries, path)
        write(EDITED, binary)

    monkeypatch.setattr(zip_json, "write", write_then_edit)

    assert cli.main(["build"]) == 1

    assert "map.aprx: refused: binary edited" in capsys.readouterr().out
    assert zip_json.read(binary) == EDITED


def test_a_write_first_restores_source_left_aside_by_a_run_that_stopped_mid_swap(project, capsys):
    record_sync(project, "map.aprx")
    (project / "map.aprx.src").rename(project / ".map.aprx.src.old")
    (project / ".map.aprx.src.new").mkdir()
    zip_json.write(EDITED, project / "map.aprx")

    assert cli.main(["sync"]) == 0

    assert capsys.readouterr().out == "map.aprx: exploded, target local\n"
    assert source.read_dir(project / "map.aprx.src") == source.render(EDITED)
    assert sorted(p.name for p in project.iterdir()) == [".git", ".gitattributes", "map.aprx", "map.aprx.src"]


def test_a_failed_rename_leaves_the_old_source_whole_and_is_reported(project, monkeypatch, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write(EDITED, binary)
    rename = Path.rename

    def held_open(self, target):
        # Fails the second rename, after the old Source has been moved aside.
        if self.name.endswith(".new"):
            raise PermissionError(13, "in use")
        return rename(self, target)

    monkeypatch.setattr(Path, "rename", held_open)

    assert cli.main(["explode"]) == 1

    assert capsys.readouterr().out.startswith("map.aprx: error: ")
    assert source.read_dir(project / "map.aprx.src") == source.render(ENTRIES)
    assert sorted(p.name for p in project.iterdir()) == [".git", ".gitattributes", "map.aprx", "map.aprx.src"]


def test_sync_without_a_record_says_how_to_pick_a_side(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")
    source.write_dir(source.render(ENTRIES), project / "map.aprx.src")

    assert cli.main(["sync"]) == 1

    assert capsys.readouterr().out.splitlines() == [
        "map.aprx: refused: no record",
        "    Run explode to keep the binary, or build --discard to keep Source.",
    ]


def test_build_refuses_when_the_target_has_no_value_for_a_placeholder(configured, capsys):
    source.write_dir(source.render(NEUTRAL), configured / "map.aprx.src")

    assert cli.main(["build", "--target", "dev"]) == 1

    assert capsys.readouterr().out.splitlines() == [
        "map.aprx: refused: placeholder problems",
        "    map/map.json: no value is set for main_gdb",
    ]
    assert not (configured / "map.aprx").exists()


def test_a_target_that_config_does_not_declare_is_an_error(configured, capsys):
    zip_json.write(ENTRIES, configured / "map.aprx")
    (configured / "cimkit.local.toml").write_text(f"[local]\nmain_gdb = '{VALUE}'\n")

    assert cli.main(["explode", "--target", "prd"]) == 1

    assert "'prd' is not a declared target" in capsys.readouterr().out
    assert not (configured / "map.aprx.src").exists()


def test_after_exploding_committed_source_the_project_is_clean_not_a_staged_edit(project, capsys):
    binary = record_sync(project, "map.aprx")
    run(project, "add", "-A")
    run(project, "commit", "-qm", "c")
    zip_json.write(EDITED, binary)

    assert cli.main(["sync"]) == 0

    capsys.readouterr()
    assert status(capsys) == ["map.aprx: clean"]
