"""git.py against real throwaway repos."""

import subprocess

import pytest

from cimkit_git import errors, git


def run(repo, *args, stdin=None):
    return subprocess.run(
        ["git", *args], cwd=repo, input=stdin, capture_output=True, check=True
    ).stdout.decode().strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "repo"
    path.mkdir()
    run(path, "init", "-q", "-b", "main")
    run(path, "config", "user.name", "t")
    run(path, "config", "user.email", "t@example.com")
    run(path, "config", "commit.gpgsign", "false")
    return path


def commit(repo, files):
    for name, data in files.items():
        path = repo / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
    run(repo, "add", "-A")
    run(repo, "commit", "-qm", "c")


FILES = {"a.json": b"{}\n", "sub/b.xml": b"<b/>\n", "sub/deep/c": b"\x00\x01"}


def test_files_tree_matches_the_tree_git_commits_for_the_same_files(repo):
    commit(repo, {f"map.aprx.src/{n}": d for n, d in FILES.items()})

    assert git.files_tree(repo, FILES) == run(repo, "rev-parse", "HEAD:map.aprx.src")


def test_files_tree_hashes_bytes_as_given_even_with_autocrlf(repo):
    run(repo, "config", "core.autocrlf", "true")

    assert git.files_tree(repo, {"a.json": b"{}\r\n"}) != git.files_tree(repo, {"a.json": b"{}\n"})


def test_index_tree_is_the_staged_source_not_the_working_copy(repo):
    commit(repo, {"map.aprx.src/a.json": b"{}\n", "other.txt": b"x"})
    (repo / "map.aprx.src/a.json").write_bytes(b"staged\n")
    run(repo, "add", "-A")
    (repo / "map.aprx.src/a.json").write_bytes(b"working\n")
    staged = run(repo, "rev-parse", run(repo, "write-tree") + ":map.aprx.src")

    assert git.index_tree(repo, "map.aprx.src") == staged


def test_index_tree_is_none_when_nothing_under_the_dir_is_staged(repo):
    commit(repo, {"other.txt": b"x"})

    assert git.index_tree(repo, "map.aprx.src") is None


def test_unmerged_lists_conflicted_paths_under_the_dir_only(repo):
    commit(repo, {"map.aprx.src/a.json": b"base\n", "other.txt": b"base\n"})
    run(repo, "switch", "-qc", "side")
    commit(repo, {"map.aprx.src/a.json": b"side\n", "other.txt": b"side\n"})
    run(repo, "switch", "-q", "main")
    commit(repo, {"map.aprx.src/a.json": b"main\n", "other.txt": b"main\n"})
    subprocess.run(["git", "merge", "-q", "side"], cwd=repo, capture_output=True)

    assert git.unmerged(repo, "map.aprx.src") == ["map.aprx.src/a.json"]


def test_unmerged_is_empty_without_a_merge(repo):
    commit(repo, {"map.aprx.src/a.json": b"base\n"})

    assert git.unmerged(repo, "map.aprx.src") == []


def test_repo_root_and_git_dir_are_found_from_a_subdirectory(repo):
    sub = repo / "maps" / "deep"
    sub.mkdir(parents=True)

    assert git.repo_root(sub) == repo.resolve()
    assert git.git_dir(sub) == (repo / ".git").resolve()


def test_git_dir_is_per_worktree(repo):
    commit(repo, {"a": b"x"})
    run(repo, "worktree", "add", "-q", "../wt")

    assert git.git_dir(repo.parent / "wt") == (repo / ".git" / "worktrees" / "wt").resolve()


def test_outside_a_repo_is_a_typed_error(tmp_path):
    with pytest.raises(errors.NotAGitRepoError):
        git.repo_root(tmp_path)


def test_converts_text_is_false_only_when_source_is_marked_minus_text(repo):
    assert git.converts_text(repo, "map.aprx.src/GISProject.json")

    (repo / ".gitattributes").write_text("**/*.aprx.src/** -text\n")

    assert not git.converts_text(repo, "map.aprx.src/GISProject.json")
    assert not git.converts_text(repo, "maps/map.aprx.src/sub/a.xml")


def test_tree_files_returns_the_exact_bytes_under_a_directory_at_a_commit(repo):
    commit(repo, {f"map.aprx.src/{n}": d for n, d in FILES.items()} | {"other.txt": b"x"})
    (repo / "map.aprx.src/a.json").write_bytes(b"working\n")

    assert git.tree_files(repo, git.rev_tree(repo, "HEAD"), "map.aprx.src") == FILES


def test_a_symlink_in_a_committed_tree_is_refused(repo):
    (repo / "map.aprx.src").mkdir()
    (repo / "map.aprx.src/link").symlink_to("/etc/passwd")
    commit(repo, {})

    with pytest.raises(errors.SymlinkInSourceError):
        git.tree_files(repo, git.rev_tree(repo, "HEAD"), "map.aprx.src")
