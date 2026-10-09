"""cimkit-git hook <name>, called directly as the shims will call it."""

import io

import pytest

from cimkit_git import cli, source
from cimkit_git.formats import zip_json
from tests.test_cli_check import SECRET, committed
from tests.test_cli_status import (  # noqa: F401
    CONFIG,
    ENTRIES,
    NEUTRAL,
    configured,
    layer,
    project,
    record_sync,
    write_source,
)
from tests.test_git import commit, repo, run  # noqa: F401


def hook(capsys, *args):
    code = cli.main(["hook", *args])
    return code, capsys.readouterr().out.splitlines()


def test_pre_commit_blocks_a_leak_in_staged_source_that_the_working_copy_lacks(configured, capsys):
    src = configured / "map.aprx.src"
    write_source(src, {**ENTRIES, "map/map.json": layer(SECRET)})
    run(configured, "add", "-A")
    (src / "map" / "map.json").write_bytes(source.render(NEUTRAL)["map/map.json"])

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert "map.aprx.src/map/map.json: error: unregistered value DATABASE=x.sde;USER=u;PASSWORD=***" in out


def test_pre_commit_blocks_a_staged_binary_and_names_the_command_that_unstages_it(project, capsys):
    record_sync(project, "map.aprx")
    run(project, "add", "-f", "-A")

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert "map.aprx: error: the binary is staged; run git rm --cached map.aprx" in out


def test_pre_commit_blocks_a_binary_edit_not_yet_in_source_and_says_to_sync(project, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write({**ENTRIES, "GISProject.json": b'{"version":"3.4.0"}'}, binary)

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert "map.aprx: error: binary edited. Run cimkit-git sync." in out


def test_pre_commit_blocks_a_binary_with_no_source_and_says_to_sync(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")

    assert hook(capsys, "pre-commit") == (1, ["map.aprx: error: new project. Run cimkit-git sync.", "1 error(s), 0 warning(s)"])


def test_pre_commit_blocks_a_project_sync_refuses_and_gives_the_way_past_it(project, capsys):
    zip_json.write(ENTRIES, project / "map.aprx")
    write_source(project / "map.aprx.src")

    assert hook(capsys, "pre-commit") == (
        1,
        ["map.aprx: error: no record. Run explode to keep the binary, or build --discard to keep Source.", "1 error(s), 0 warning(s)"],
    )


def test_pre_commit_blocks_a_conflict(project, capsys):
    binary = record_sync(project, "map.aprx")
    zip_json.write({**ENTRIES, "GISProject.json": b'{"version":"3.4.0"}'}, binary)
    (project / "map.aprx.src" / "map" / "map.json").write_bytes(source.render(NEUTRAL)["map/map.json"])

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert out[0].startswith("map.aprx: error: conflict. Run explode --force")


def test_pre_commit_blocks_while_git_would_convert_line_endings_in_source(repo, monkeypatch, capsys):
    monkeypatch.chdir(repo)
    write_source(repo / "map.aprx.src")

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert out[:2] == ["map.aprx: error: git converts line endings in Source. Run cimkit-git install, or add this line to .gitattributes so git never converts Source:", "        **/*.aprx.src/** -text"]


def test_pre_commit_blocks_staged_source_that_matches_neither_the_working_copy_nor_the_record(project, capsys):
    record_sync(project, "map.aprx")
    gis = project / "map.aprx.src" / "GISProject.json"
    original = gis.read_bytes()
    gis.write_text('{\n  "version": "3.4.0"\n}\n')
    run(project, "add", "map.aprx.src")
    gis.write_bytes(original)

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert out[0].startswith("map.aprx: error: staged edit. The index holds a Source edit")


def test_pre_commit_passes_source_edits_made_without_a_binary_since_no_pro_edit_can_be_lost(project, capsys):
    (project / ".gitignore").write_text("*.aprx\n")
    record_sync(project, "a.aprx", NEUTRAL)
    (project / "a.aprx.src" / "GISProject.json").write_text('{\n  "version": "3.4.0"\n}\n')
    write_source(project / "b.aprx.src", NEUTRAL)
    run(project, "add", "-A")

    assert hook(capsys, "pre-commit") == (0, ["0 error(s), 0 warning(s)"])


def test_pre_commit_blocks_on_a_project_it_cannot_read_and_still_checks_the_rest(project, capsys):
    (project / "bad.aprx").write_bytes(b"not a zip")
    zip_json.write(ENTRIES, project / "good.aprx")

    code, out = hook(capsys, "pre-commit")

    assert code == 1
    assert out[0].startswith("bad.aprx: error: ")
    assert out[1] == "good.aprx: error: new project. Run cimkit-git sync."


ZERO = "0" * 40


@pytest.fixture
def remote(configured, tmp_path):
    """configured, with an origin that already holds one clean commit on main."""
    run(tmp_path, "init", "-q", "--bare", "origin.git")
    run(configured, "remote", "add", "origin", str(tmp_path / "origin.git"))
    committed(configured, "map.aprx.src", NEUTRAL, **{"cimkit.toml": CONFIG.encode()})
    run(configured, "push", "-q", "origin", "main")
    return configured


def push(capsys, monkeypatch, *refs):
    """Run pre-push as git does: remote name and URL as arguments, one ref line per update."""
    monkeypatch.setattr("sys.stdin", io.StringIO("".join(" ".join(r) + "\n" for r in refs)))
    return hook(capsys, "pre-push", "origin", "unused-url")


def leak(repo):
    committed(repo, "map.aprx.src", {**ENTRIES, "map/map.json": layer(SECRET)})
    return run(repo, "rev-parse", "HEAD")


def test_pre_push_blocks_an_outgoing_commit_with_a_leak_even_when_a_later_one_fixes_it(remote, monkeypatch, capsys):
    base = run(remote, "rev-parse", "HEAD")
    bad = leak(remote)
    committed(remote, "map.aprx.src", NEUTRAL)
    tip = run(remote, "rev-parse", "HEAD")

    code, out = push(capsys, monkeypatch, ("refs/heads/main", tip, "refs/heads/main", base))

    assert code == 1
    assert out == [
        f"commit {bad}:",
        "    map.aprx.src/map/map.json: error: unregistered value DATABASE=x.sde;USER=u;PASSWORD=***",
        "1 of 2 outgoing commit(s) fail check",
    ]


def test_pre_push_ignores_a_branch_delete(remote, monkeypatch, capsys):
    base = run(remote, "rev-parse", "HEAD")

    assert push(capsys, monkeypatch, ("(delete)", ZERO, "refs/heads/main", base)) == (0, [])


def test_pre_push_of_a_new_branch_checks_only_the_commits_origin_lacks(remote, monkeypatch, capsys):
    (remote / "map.aprx.src" / "GISProject.json").write_bytes(b"{")
    run(remote, "commit", "-qam", "broken, and already on origin")
    run(remote, "push", "-q", "origin", "main")
    run(remote, "switch", "-qc", "feature")
    bad = leak(remote)

    code, out = push(capsys, monkeypatch, ("refs/heads/feature", bad, "refs/heads/feature", ZERO))

    assert code == 1
    assert out[0] == f"commit {bad}:"
    assert out[-1] == "1 of 1 outgoing commit(s) fail check"


def test_pre_push_blocks_a_commit_it_cannot_check_and_names_it(remote, monkeypatch, capsys):
    base = run(remote, "rev-parse", "HEAD")
    commit(remote, {"cimkit.toml": b"[git\n"})
    bad = run(remote, "rev-parse", "HEAD")

    code, out = push(capsys, monkeypatch, ("refs/heads/main", bad, "refs/heads/main", base))

    assert code == 1
    assert out[0] == f"commit {bad}:"
    assert out[1].startswith("    error: ")


def test_post_checkout_warns_of_source_the_binary_lacks_and_says_to_sync(project, capsys):
    record_sync(project, "map.aprx")
    (project / "map.aprx.src" / "GISProject.json").write_text('{\n  "version": "3.4.0"\n}\n')

    assert hook(capsys, "post-checkout", ZERO, ZERO, "1") == (0, ["map.aprx: Source changed. Run cimkit-git sync."])


@pytest.mark.parametrize("args", [["post-merge", "0"], ["post-rewrite", "rebase"], ["post-stash"]])
def test_the_other_post_hooks_warn_the_same_way(project, capsys, args):
    zip_json.write(ENTRIES, project / "map.aprx")

    assert hook(capsys, *args) == (0, ["map.aprx: new project. Run cimkit-git sync."])


def test_a_post_hook_warns_of_a_project_it_cannot_read_and_still_reports_the_rest(project, capsys):
    (project / "bad.aprx").write_bytes(b"not a zip")
    zip_json.write(ENTRIES, project / "good.aprx")

    code, out = hook(capsys, "post-merge", "0")

    assert code == 0
    assert out[0].startswith("bad.aprx: error: ")
    assert out[1] == "good.aprx: new project. Run cimkit-git sync."


def test_a_post_hook_that_cannot_run_says_why_and_still_exits_0(project, capsys):
    (project / "cimkit.toml").write_text("[git\n")

    assert cli.main(["hook", "post-merge", "0"]) == 0
    assert capsys.readouterr().err.startswith("cimkit-git: ")


def test_hook_is_left_out_of_the_help(capsys):
    with pytest.raises(SystemExit):
        cli.main(["--help"])

    assert "hook" not in capsys.readouterr().out


def test_pre_commit_blocks_when_it_cannot_run(project, capsys):
    (project / "cimkit.toml").write_text("[git\n")

    assert cli.main(["hook", "pre-commit"]) == 1
    assert capsys.readouterr().err.startswith("cimkit-git: ")
