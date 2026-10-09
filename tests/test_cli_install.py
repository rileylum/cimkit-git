"""cimkit-git install, checked by letting git run the hooks it registers."""

import subprocess
from pathlib import Path

import pytest

import cimkit_git
from cimkit_git import cli
from cimkit_git.formats import zip_json
from tests.test_cli_hooks import leak, remote  # noqa: F401
from tests.test_cli_status import ENTRIES, configured, project  # noqa: F401
from tests.test_git import repo, run  # noqa: F401


@pytest.fixture
def importable(monkeypatch):
    """Lets the Python git starts for a hook import this checkout. Without it, a run with
    no installed package (uv run --no-project) finds cimkit_git only through pytest's path."""
    monkeypatch.setenv("PYTHONPATH", str(Path(cimkit_git.__file__).parent.parent))


def install(capsys):
    code = cli.main(["install"])
    return code, capsys.readouterr().out.splitlines()


def git_commit(repo):
    return subprocess.run(["git", "commit", "-qm", "c"], cwd=repo, capture_output=True, text=True, check=False)


def test_git_runs_the_registered_pre_commit_hook_and_it_blocks_an_unsynced_project(project, capsys, importable):
    install(capsys)
    zip_json.write(ENTRIES, project / "map.aprx")
    run(project, "add", ".gitattributes")

    result = git_commit(project)

    assert result.returncode == 1
    assert "map.aprx: error: new project. Run cimkit-git sync." in result.stdout + result.stderr


def test_git_passes_the_remote_and_refs_to_the_registered_pre_push_hook_and_it_blocks_a_leak(remote, capsys, importable):
    # Before install, or pre-commit would refuse the leak.
    leak(remote)
    install(capsys)

    result = subprocess.run(["git", "push", "origin", "main"], cwd=remote, capture_output=True, text=True, check=False)

    assert result.returncode == 1
    assert "1 of 1 outgoing commit(s) fail check" in result.stdout + result.stderr


def test_installing_twice_leaves_one_event_entry_per_hook_in_git_config(project, capsys):
    install(capsys)
    install(capsys)

    # git hook list hides a repeated entry, so read the config itself.
    assert run(project, "config", "--get-all", "hook.cimkit-git-pre-commit.event") == "pre-commit"


def test_installing_again_records_the_python_it_now_runs_under_quoted_for_sh(project, capsys, monkeypatch):
    install(capsys)
    monkeypatch.setattr("sys.executable", "/opt/Pro Python/python")
    install(capsys)

    command = run(project, "config", "hook.cimkit-git-pre-push.command")

    assert command == "'/opt/Pro Python/python' -m cimkit_git.cli hook pre-push"


def test_on_git_before_2_54_git_runs_the_shim_install_wrote_and_it_blocks_an_unsynced_project(project, capsys, importable, monkeypatch):
    monkeypatch.setattr("cimkit_git.git.version", lambda: (2, 53))
    install(capsys)
    zip_json.write(ENTRIES, project / "map.aprx")
    run(project, "add", ".gitattributes")

    result = git_commit(project)

    assert result.returncode == 1
    assert "map.aprx: error: new project. Run cimkit-git sync." in result.stdout + result.stderr
    # Older git reads no hook config, so the hook that ran must be the shim.
    assert run(project, "hook", "list", "pre-commit") == "hook from hookdir"


def test_on_git_before_2_54_install_leaves_a_hook_it_did_not_write_and_says_how_to_call_cimkit_git_from_it(project, capsys, monkeypatch):
    monkeypatch.setattr("cimkit_git.git.version", lambda: (2, 53))
    monkeypatch.setattr("sys.executable", "/py")
    theirs = project / ".git" / "hooks" / "pre-commit"
    theirs.write_text("#!/bin/sh\nmake lint\n")

    code, out = install(capsys)

    assert code == 1
    assert theirs.read_text() == "#!/bin/sh\nmake lint\n"
    assert out == [
        ".git/hooks/pre-commit: error: not written by cimkit-git, so install left it alone. Add this line to it:",
        '    /py -m cimkit_git.cli hook pre-commit "$@"',
    ]
    assert run(project, "hook", "list", "pre-push") == "hook from hookdir"


def test_on_git_before_2_54_install_names_a_hook_outside_the_repo_by_its_full_path(project, capsys, monkeypatch, tmp_path):
    monkeypatch.setattr("cimkit_git.git.version", lambda: (2, 53))
    shared = tmp_path / "shared-hooks"
    shared.mkdir()
    (shared / "pre-push").write_text("#!/bin/sh\n")
    run(project, "config", "core.hooksPath", str(shared))

    code, out = install(capsys)

    assert code == 1
    assert out[0] == f"{(shared / 'pre-push').as_posix()}: error: not written by cimkit-git, so install left it alone. Add this line to it:"


def test_on_git_before_2_54_installing_again_rewrites_its_own_shim_with_the_new_python(project, capsys, monkeypatch):
    monkeypatch.setattr("cimkit_git.git.version", lambda: (2, 53))
    monkeypatch.setattr("sys.executable", "/old/python")
    install(capsys)
    monkeypatch.setattr("sys.executable", "/new/python")

    code, _ = install(capsys)

    assert code == 0
    assert 'exec /new/python -m cimkit_git.cli hook post-merge "$@"' in (project / ".git" / "hooks" / "post-merge").read_text()


def test_install_writes_a_gitignore_that_ignores_the_binary_and_the_local_values(project, capsys):
    install(capsys)

    assert (project / ".gitignore").read_text() == "*.aprx\ncimkit.local.toml\n"


def test_install_adds_only_the_ignore_lines_a_gitignore_lacks_and_keeps_the_rest(project, capsys):
    (project / ".gitignore").write_text("build/\ncimkit.local.toml")

    install(capsys)

    assert (project / ".gitignore").read_text() == "build/\ncimkit.local.toml\n*.aprx\n"


def test_install_keeps_the_line_endings_of_the_gitignore_it_appends_to(project, capsys):
    (project / ".gitignore").write_bytes(b"build/\r\n")

    install(capsys)

    assert (project / ".gitignore").read_bytes() == b"build/\r\n*.aprx\ncimkit.local.toml\n"


def test_install_tells_git_never_to_convert_line_endings_in_source(repo, capsys, monkeypatch):
    monkeypatch.chdir(repo)

    install(capsys)

    assert (repo / ".gitattributes").read_text() == "**/*.aprx.src/** -text\n"
