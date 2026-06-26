import json
import shutil

import pytest

from aprx_tools.explode import explode
from aprx_tools.verify import verify


# --------------------------------------------------------------------------- #
# Environment-managed projects
# --------------------------------------------------------------------------- #

def _src(env_project):
    return env_project.dir / "map.aprx.src"


def test_verify_env_passes(env_project, explode_env):
    explode_env(env_project.aprx)             # produces tokenised source
    assert verify(str(_src(env_project))) == 0


def test_verify_fails_when_env_missing_key(env_project, explode_env):
    explode_env(env_project.aprx)
    (env_project.dir / "connections" / "uat.json").write_text("{}")   # drop the key
    assert verify(str(_src(env_project))) == 1


def test_verify_malformed_connections_file_is_collected_not_aborting(
    env_project, explode_env, capsys
):
    # Issue 0009: a hand-broken / merge-conflicted connections file makes
    # load_connections hard-exit (pre-0009 it raised an uncaught JSONDecodeError that
    # crashed the gate). The per-project catch now collects it as one project's problem
    # — verify still returns its FAILED exit code with a diagnostic, not a traceback.
    explode_env(env_project.aprx)
    (env_project.dir / "connections" / "uat.json").write_text("{ not valid json")
    assert verify(str(_src(env_project))) == 1
    err = capsys.readouterr().err
    assert "map.aprx.src" in err          # the offending project is named
    assert "valid JSON" in err            # ...with the malformed-content diagnostic


def test_verify_env_missing_binary_is_fine(env_project, explode_env):
    """Scope guard: in environment mode the working `.aprx` is a gitignored build
    artifact (PRD story 18 / ADR-0001), regenerated per environment from neutral
    source. Its absence is the *correct* committed state, so the missing-binary rule
    (simple mode only, issue 0012) must never fire here — verify stays green."""
    explode_env(env_project.aprx)
    env_project.aprx.unlink()                 # the build artifact is not committed
    assert verify(str(_src(env_project))) == 0


def test_verify_specific_env(env_project, explode_env):
    explode_env(env_project.aprx)
    assert verify(str(_src(env_project)), env="uat") == 0
    (env_project.dir / "connections" / "uat.json").write_text("{}")
    assert verify(str(_src(env_project)), env="uat") == 1


def test_verify_fails_on_raw_connection_string(env_project, explode_env):
    src = explode_env(env_project.aprx)
    pts = src / "map" / "test_points.json"
    # Simulate a commit made without the hooks: a raw connection string in source.
    raw_json = json.dumps(env_project.value)[1:-1]   # JSON-escaped, quotes stripped
    pts.write_text(pts.read_text().replace("@@main@@", raw_json))
    assert verify(str(src)) == 1


# --------------------------------------------------------------------------- #
# Simple (single-environment) projects
# --------------------------------------------------------------------------- #

def _simple_project(base, simple_aprx, **policy):
    """Lay out a simple-mode Project under *base*: the .aprx plus the committed
    `aprx.json` that declares `mode: simple`. Strict resolution (ADR-0001) means verify
    reads that file rather than guessing, so it must be present in the working tree.

    Extra **policy keys (e.g. ``commit_binary=True``) are written into the aprx.json so
    a test can opt the Project into the binary-lifecycle policy under test."""
    base.mkdir(parents=True, exist_ok=True)
    aprx = base / "simple.aprx"
    shutil.copy(simple_aprx, aprx)
    (base / "aprx.json").write_text(json.dumps({"mode": "simple", **policy}))
    return aprx


def test_verify_simple_in_sync(tmp_path, simple_aprx):
    aprx = _simple_project(tmp_path, simple_aprx)
    explode(str(aprx))
    assert verify(str(tmp_path / "simple.aprx.src")) == 0


def test_verify_simple_out_of_sync(tmp_path, simple_aprx):
    aprx = _simple_project(tmp_path, simple_aprx)
    src = explode(str(aprx))
    gp = src / "GISProject.json"
    data = json.loads(gp.read_text())
    data["__tamper__"] = True                  # source no longer matches the binary
    gp.write_text(json.dumps(data))
    assert verify(str(src)) == 1


def test_verify_simple_missing_binary_is_fine(tmp_path, simple_aprx):
    """Sync-if-present (issue 0001): a simple-mode Project whose committed binary is
    absent is a legitimate Source-only state — the Source is the canonical truth and
    the `.aprx` a regenerated artifact — so verify PASSES rather than failing on the
    missing binary (the pre-0001 hard failure). The dropped "forgot to commit the
    binary" detection is what issue 0003's `commit_binary: true` opt-in restores."""
    aprx = _simple_project(tmp_path, simple_aprx)
    src = explode(str(aprx))
    aprx.unlink()                              # binary hand-ignored / never committed
    assert verify(str(src)) == 0


def test_verify_simple_commit_binary_true_missing_binary_fails(tmp_path, simple_aprx):
    """`commit_binary: true` is the opt-in that restores the incomplete-commit
    detection 0001 relaxed: a simple-mode Project that declares it must commit the
    binary FAILS verify when the binary is absent. Simple mode can always build a
    faithful, neutral binary, so its absence under this policy is a forgotten commit."""
    aprx = _simple_project(tmp_path, simple_aprx, commit_binary=True)
    src = explode(str(aprx))
    aprx.unlink()                              # the policy says commit it, but it is gone
    assert verify(str(src)) == 1


def test_verify_simple_commit_binary_true_present_in_sync_passes(tmp_path, simple_aprx):
    """The other half of the opt-in: with `commit_binary: true` and a present, in-sync
    binary, verify PASSES — the policy is satisfied, and the present binary is still
    packed-and-compared (so a stale one would still fail)."""
    aprx = _simple_project(tmp_path, simple_aprx, commit_binary=True)
    explode(str(aprx))
    assert verify(str(tmp_path / "simple.aprx.src")) == 0


def test_verify_simple_commit_binary_false_missing_binary_passes(tmp_path, simple_aprx):
    """`commit_binary: false` is a *declared* no-commit policy: a missing binary is the
    intended state, so verify stays green — same sync-if-present behaviour as absent."""
    aprx = _simple_project(tmp_path, simple_aprx, commit_binary=False)
    src = explode(str(aprx))
    aprx.unlink()
    assert verify(str(src)) == 0


def test_verify_simple_problems_collected_not_aborting(
    tmp_path, simple_aprx, monkeypatch, capsys
):
    """As the repo-wide gate, each Project's failure is one collected problem, not a
    loop-abort: sibling Projects are still checked (consistent with 0007). Exercised
    through a real whole-tree run (`verify()` with no src_dir, discovering both
    Projects via `iter_src_dirs`) so the multi-target loop is actually driven — a
    single-target call would only ever iterate once and prove nothing about collection.
    Both Projects carry their own out-of-sync problem, so BOTH must surface: that the
    second is reported is the proof the first did not abort the loop. (Missing binary
    is no longer a problem under issue 0001's sync-if-present, so the two failures here
    are stale committed binaries — the case verify still catches.)"""
    for name in ("one", "two"):
        src = explode(str(_simple_project(tmp_path / name, simple_aprx)))
        gp = src / "GISProject.json"
        data = json.loads(gp.read_text())
        data["__tamper__"] = True               # source no longer matches the committed binary
        gp.write_text(json.dumps(data))

    # No src_dir → discover every Project under the root. tmp_path is not a git repo,
    # so git_root(required=False) falls back to cwd; point cwd at the tree holding both.
    monkeypatch.chdir(tmp_path)
    assert verify() == 1
    err = capsys.readouterr().err
    assert "2 problem(s)" in err                # both Projects checked — neither aborted the loop
    assert err.count("out of sync") == 2        # each sibling reported its own problem


# --------------------------------------------------------------------------- #
# Unresolved projects (no declared Mode)
# --------------------------------------------------------------------------- #

def test_verify_unresolved_project_directs_to_install(tmp_path, simple_aprx, capsys):
    """A Project with no `aprx.json` declares no Mode. Strict resolution must surface
    the "run `aprx install`" guidance rather than silently passing (ADR-0001) — as a
    collected failure (exit 1), not a loop-aborting hard exit, so the repo-wide gate
    keeps checking every other project."""
    aprx = tmp_path / "simple.aprx"
    shutil.copy(simple_aprx, aprx)
    src = explode(str(aprx))                    # source exists, but no aprx.json beside it
    assert verify(str(src)) == 1
    assert "aprx install" in capsys.readouterr().err
