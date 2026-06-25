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

def _simple_project(base, simple_aprx):
    """Lay out a simple-mode Project under *base*: the .aprx plus the committed
    `aprx.json` that declares `mode: simple`. Strict resolution (ADR-0001) means verify
    reads that file rather than guessing, so it must be present in the working tree."""
    base.mkdir(parents=True, exist_ok=True)
    aprx = base / "simple.aprx"
    shutil.copy(simple_aprx, aprx)
    (base / "aprx.json").write_text(json.dumps({"mode": "simple"}))
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


def test_verify_simple_missing_binary(tmp_path, simple_aprx, capsys):
    """A simple-mode Project commits both the Source and the binary. Source present
    with the `.aprx` absent is an incomplete commit, not a valid state: the in-sync
    gate (PRD story 20) has nothing to rebuild against, so verify must FAIL and name
    the remediation rather than wave it through (the pre-0007 silent `return`)."""
    aprx = _simple_project(tmp_path, simple_aprx)
    src = explode(str(aprx))
    aprx.unlink()                              # binary never committed / deleted
    assert verify(str(src)) == 1
    err = capsys.readouterr().err
    # Name the missing *binary* specifically — "simple.aprx" alone would also be
    # satisfied by the source dir "simple.aprx.src", so assert it in its message slot.
    assert "committed simple.aprx is missing" in err
    assert "aprx pack" in err                  # points at the remediation


def test_verify_simple_missing_binary_collected_not_aborting(
    tmp_path, simple_aprx, monkeypatch, capsys
):
    """As the repo-wide gate, a missing binary is one collected problem, not a
    loop-abort: sibling Projects are still checked (consistent with 0007). Exercised
    through a real whole-tree run (`verify()` with no src_dir, discovering both
    Projects via `iter_src_dirs`) so the multi-target loop is actually driven — a
    single-target call would only ever iterate once and prove nothing about collection.
    The sibling carries its own distinct out-of-sync problem, so BOTH must surface:
    that the second problem is reported is the proof the first one did not abort the loop."""
    _simple_project(tmp_path / "missing", simple_aprx)
    missing_aprx = explode(str(tmp_path / "missing" / "simple.aprx")).parent / "simple.aprx"
    missing_aprx.unlink()                       # problem 1: binary is missing

    stale_src = explode(str(_simple_project(tmp_path / "stale", simple_aprx)))
    gp = stale_src / "GISProject.json"
    data = json.loads(gp.read_text())
    data["__tamper__"] = True                   # problem 2: source no longer matches binary
    gp.write_text(json.dumps(data))

    # No src_dir → discover every Project under the root. tmp_path is not a git repo,
    # so git_root(required=False) falls back to cwd; point cwd at the tree holding both.
    monkeypatch.chdir(tmp_path)
    assert verify() == 1
    err = capsys.readouterr().err
    assert "2 problem(s)" in err                # both Projects checked — neither aborted the loop
    assert "committed simple.aprx is missing" in err
    assert "out of sync" in err


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
