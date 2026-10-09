"""cimkit-git check, in throwaway repos."""

from cimkit_git import cli, source
from tests.test_cli_status import (  # noqa: F401
    CONFIG,
    ENTRIES,
    NEUTRAL,
    configured,
    layer,
    project,
    write_source,
)
from tests.test_git import commit, repo  # noqa: F401

SECRET = "DATABASE=x.sde;USER=u;PASSWORD=hunter2"


def check(capsys, *args):
    code = cli.main(["check", *args])
    return code, capsys.readouterr().out.splitlines()


def test_a_real_value_in_source_is_an_error_printed_masked(configured, capsys):
    write_source(configured / "map.aprx.src", {**ENTRIES, "map/map.json": layer(SECRET)})

    code, out = check(capsys)

    assert code == 1
    assert out[0] == "map.aprx.src/map/map.json: error: unregistered value DATABASE=x.sde;USER=u;PASSWORD=***"
    assert "hunter2" not in "\n".join(out)


def test_machine_paths_warn_after_the_errors_and_leave_the_exit_code_alone(project, capsys):
    write_source(project / "a.aprx.src")
    write_source(project / "b.aprx.src")
    (project / "b.aprx.src" / "y.json").write_bytes(b'{"x": ')

    code, out = check(capsys)

    assert out[0].startswith("b.aprx.src/y.json: error: ")
    assert out[1:] == [
        r"a.aprx.src/map/map.json: warning: 1 machine path(s), first C:\x.gdb",
        r"b.aprx.src/map/map.json: warning: 1 machine path(s), first C:\x.gdb",
        "1 error(s), 2 warning(s)",
    ]
    assert code == 1


def test_warnings_alone_pass(project, capsys):
    write_source(project / "a.aprx.src")

    assert check(capsys) == (0, [r"a.aprx.src/map/map.json: warning: 1 machine path(s), first C:\x.gdb", "0 error(s), 1 warning(s)"])


def test_with_target_each_declared_key_the_target_lacks_is_an_error(configured, monkeypatch, capsys):
    write_source(configured / "map.aprx.src", NEUTRAL)
    (configured / "cimkit.local.toml").write_text("[local]\nmain_gdb = 'x'\n")

    assert check(capsys, "--target", "local") == (0, ["0 error(s), 0 warning(s)"])
    assert check(capsys, "--target", "dev") == (1, ["cimkit.toml: error: dev has no value for main_gdb", "1 error(s), 0 warning(s)"])
    monkeypatch.setenv("CIMKIT__DEV__MAIN_GDB", "y")
    assert check(capsys, "--target", "dev") == (0, ["0 error(s), 0 warning(s)"])


def test_without_target_a_broken_local_file_does_not_stop_the_check(configured, capsys):
    write_source(configured / "map.aprx.src", NEUTRAL)
    (configured / "cimkit.local.toml").write_text("[nope]\n")

    assert check(capsys) == (0, ["0 error(s), 0 warning(s)"])


def test_an_undeclared_target_or_no_targets_at_all_fails_with_a_message(project, capsys):
    assert cli.main(["check", "--target", "dev"]) == 1
    assert "declares no targets" in capsys.readouterr().err
    (project / "cimkit.toml").write_text(CONFIG)

    assert cli.main(["check", "--target", "prd"]) == 1
    assert "'prd' is not a declared target" in capsys.readouterr().err


def committed(repo, name, entries, **extra):
    commit(repo, {**{f"{name}/{n}": d for n, d in source.render(entries).items()}, **extra})


def test_with_rev_check_reads_the_commit_not_the_working_tree(configured, capsys):
    committed(configured, "map.aprx.src", ENTRIES)
    write_source(configured / "fixed.aprx.src", NEUTRAL)
    (configured / "map.aprx.src" / "map" / "map.json").write_bytes(source.render(NEUTRAL)["map/map.json"])

    code, out = check(capsys, "--rev", "HEAD")

    assert code == 1
    assert out[0].startswith("map.aprx.src/map/map.json: error: unregistered value")
    assert out[-1] == "1 error(s), 0 warning(s)"
    assert check(capsys) == (0, ["0 error(s), 0 warning(s)"])


def test_with_rev_check_uses_the_config_committed_in_it(configured, capsys):
    committed(configured, "map.aprx.src", NEUTRAL)
    (configured / "cimkit.toml").write_text(CONFIG.replace('"main_gdb"', '"other_gdb"'))

    assert check(capsys, "--rev", "HEAD") == (0, ["0 error(s), 0 warning(s)"])
    assert check(capsys)[0] == 1


def test_with_rev_projects_are_found_under_the_committed_config_minus_exclude_and_hidden_dirs(
    project, monkeypatch, capsys
):
    files = {}
    for name in ["outside", "gis/a", "gis/archive/b", "gis/.backup/c"]:
        files.update({f"{name}.aprx.src/{n}": d for n, d in source.render(ENTRIES).items()})
    commit(project, {**files, "gis/cimkit.toml": b'[git]\nexclude = ["archive/*"]\n'})
    # Gone from disk, so only the commit can supply it.
    (project / "gis" / "cimkit.toml").unlink()
    (project / "gis" / "sub").mkdir()
    monkeypatch.chdir(project / "gis" / "sub")

    assert check(capsys, "--rev", "HEAD") == (
        0,
        [r"gis/a.aprx.src/map/map.json: warning: 1 machine path(s), first C:\x.gdb", "0 error(s), 1 warning(s)"],
    )


def test_an_unknown_rev_fails_with_a_message(project, capsys):
    assert cli.main(["check", "--rev", "nope"]) == 1
    assert "nope" in capsys.readouterr().err


def test_a_project_that_cannot_be_read_is_an_error_and_the_rest_are_still_checked(project, capsys):
    write_source(project / "a.aprx.src", NEUTRAL)
    write_source(project / "b.aprx.src")
    (project / "a.aprx.src" / "link").symlink_to(project / "b.aprx.src" / "GISProject.json")

    code, out = check(capsys)

    assert code == 1
    assert out[0].startswith("a.aprx.src: error: ") and "symlink" in out[0]
    assert out[-1] == "1 error(s), 1 warning(s)"
