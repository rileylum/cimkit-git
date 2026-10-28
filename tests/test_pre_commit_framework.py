""".pre-commit-hooks.yaml, run by the pre-commit framework against this checkout.

try-repo installs the checkout into the framework's own virtualenv and runs the hook from
there, so these tests also prove the cimkit-git entry point the hooks file names. It reads
only tracked files, so a new hooks file must be staged before these tests see it.
"""

import os
import shutil
import subprocess
from pathlib import Path

import pytest

import cimkit_git
from cimkit_git.formats import zip_json
from tests.test_cli_hooks import leak, remote  # noqa: F401
from tests.test_cli_status import ENTRIES, configured, project  # noqa: F401
from tests.test_git import repo, run  # noqa: F401

pytestmark = pytest.mark.skipif(shutil.which("pre-commit") is None, reason="pre-commit is not installed")

CHECKOUT = Path(cimkit_git.__file__).parent.parent


def try_repo(repo, tmp_path, hook_id, *args):
    result = subprocess.run(
        ["pre-commit", "try-repo", str(CHECKOUT), hook_id, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        env={**os.environ, "PRE_COMMIT_HOME": str(tmp_path / "pre-commit-home")},
        check=False,
    )
    return result.returncode, result.stdout + result.stderr


def test_the_pre_commit_hook_blocks_a_staged_binary(project, tmp_path):
    zip_json.write(ENTRIES, project / "map.aprx")
    run(project, "add", "-f", "map.aprx")

    code, out = try_repo(project, tmp_path, "cimkit-git-pre-commit", "--hook-stage", "pre-commit")

    assert code == 1
    assert "map.aprx: error: the binary is staged; run git rm --cached map.aprx" in out


def test_the_pre_push_hook_blocks_the_leak_in_the_ref_the_framework_passes(remote, tmp_path):
    base = run(remote, "rev-parse", "HEAD")
    bad = leak(remote)

    code, out = try_repo(
        remote, tmp_path, "cimkit-git-pre-push", "--hook-stage", "pre-push",
        # The framework sets the remote name only when all four of these are given, as its
        # git hook always does.
        "--remote-name", "origin", "--remote-url", "unused-url",
        "--remote-branch", "refs/heads/main", "--local-branch", "refs/heads/main",
        "--from-ref", base, "--to-ref", bad,
    )

    assert code == 1
    assert f"commit {bad}:" in out


def test_a_post_hook_shows_its_warning_though_it_passes(project, tmp_path):
    zip_json.write(ENTRIES, project / "map.aprx")

    code, out = try_repo(project, tmp_path, "cimkit-git-post-merge", "--hook-stage", "post-merge")

    assert code == 0
    assert "map.aprx: new project. Run cimkit-git sync." in out
