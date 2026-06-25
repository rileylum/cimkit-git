"""Hook-entry-point tests for the per-project Mode cutover (issue 0009).

These drive the *real* hook functions (`hook_pre_commit`, `hook_pre_push`) against a
throwaway git repo, asserting the safety properties the PRD calls the highest-value
coverage in the series:

  * an environment-mode Project's pre-commit re-explodes its working binary into
    **neutral** (tokenised) source and stages only that — a raw connection string in
    the working `.aprx` must never reach the staged source, and the binary is never
    staged;
  * a simple-mode Project's pre-commit packs the staged source and stages the binary,
    and is *not* mis-detected as env-managed now that it too carries an `aprx.json`;
  * a monorepo mixing both Modes handles each by its own declared Mode in one run;
  * pre-push returns the `verify` exit code, blocking an untokenised source.

Mode is read from each Project's committed `aprx.json` via `ProjectConfig`
(ADR-0001) — never sniffed from the presence of stray files.
"""

import json
import shutil
import subprocess
import zipfile
from pathlib import Path
from types import SimpleNamespace

import pytest

from aprx_tools.explode import explode
from aprx_tools.hooks import hook_pre_commit, hook_pre_push
from aprx_tools.transform import explode_transform

FIXTURES = Path(__file__).parent / "fixtures"
SIMPLE_APRX = FIXTURES / "simple" / "simple.aprx"


# --------------------------------------------------------------------------- #
# Helpers — a real git repo and Projects of each Mode inside it.
# --------------------------------------------------------------------------- #

def _git(repo, *args) -> subprocess.CompletedProcess:
    return subprocess.run(["git", *args], cwd=repo, check=True,
                          capture_output=True, text=True)


@pytest.fixture
def repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    _git(tmp_path, "config", "user.email", "t@example.com")
    _git(tmp_path, "config", "user.name", "Tester")
    # An initial commit so HEAD exists — pre-commit's `git reset HEAD <path>` unstage
    # needs a HEAD to reset against, which the normal commit-atop-history case has.
    _git(tmp_path, "commit", "--allow-empty", "-m", "init")
    return tmp_path


def _conn_value(aprx: Path) -> str:
    """The real connection string baked into the fixture binary."""
    from aprx_tools.connections import collect_field_values
    with zipfile.ZipFile(aprx) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                vals = collect_field_values(json.loads(zf.read(name)))
                if vals:
                    return sorted(vals)[0]
    raise AssertionError("fixture has no connection strings")


def _make_env_project(repo: Path, name: str) -> SimpleNamespace:
    """An environment-mode Project: aprx.json (mode env), a committed connections/
    env file + local.json mapping `main` to the fixture's real value, and a neutral
    source tree already exploded (the developer ran explode once). The working .aprx
    still carries the *raw* connection string — the thing the hook must never leak."""
    proj = repo / name
    (proj / "connections").mkdir(parents=True)
    aprx = proj / f"{name}.aprx"
    shutil.copy(SIMPLE_APRX, aprx)
    value = _conn_value(aprx)
    (proj / "aprx.json").write_text(json.dumps({"mode": "env"}))
    (proj / "connections" / "dev.json").write_text(json.dumps({"main": value}))
    (proj / "local.json").write_text(json.dumps({"main": value}))
    src = explode(str(aprx), str(proj / f"{name}.aprx.src"),
                  transform=explode_transform(proj))
    return SimpleNamespace(proj=proj, aprx=aprx, src=Path(src), value=value)


def _make_simple_project(repo: Path, name: str) -> SimpleNamespace:
    """A simple-mode Project: aprx.json (mode simple) + the binary. The binary is the
    committed source of truth here; the hook explodes it and re-stages a normalised
    copy alongside the source."""
    proj = repo / name
    proj.mkdir()
    aprx = proj / f"{name}.aprx"
    shutil.copy(SIMPLE_APRX, aprx)
    (proj / "aprx.json").write_text(json.dumps({"mode": "simple"}))
    return SimpleNamespace(proj=proj, aprx=aprx)


def _staged_names(repo: Path) -> set:
    out = _git(repo, "diff", "--cached", "--name-only").stdout
    return set(out.splitlines()) if out.strip() else set()


def _staged_contains(repo: Path, text: str) -> bool:
    """True iff *text* appears in any staged (indexed) blob."""
    proc = subprocess.run(["git", "grep", "--cached", "-F", "-q", text],
                          cwd=repo, capture_output=True)
    return proc.returncode == 0


# --------------------------------------------------------------------------- #
# pre-commit — environment mode (the leak guard)
# --------------------------------------------------------------------------- #

def test_env_precommit_stages_neutral_source_never_raw(repo, monkeypatch):
    # The safety property: the working .aprx holds a raw connection string, and the
    # pre-commit hook must stage tokenised source — the raw value must never reach the
    # index. (A bare explode() — the 0004 leak — would stage the raw string here.)
    p = _make_env_project(repo, "map")
    monkeypatch.chdir(repo)

    hook_pre_commit()

    assert _staged_contains(repo, "@@main@@")        # source was tokenised
    assert not _staged_contains(repo, p.value)       # raw string never staged


def test_env_precommit_never_stages_the_binary(repo, monkeypatch):
    # The env-mode working .aprx is a gitignored build artifact; pre-commit stages
    # source only, never the binary.
    _make_env_project(repo, "map")
    monkeypatch.chdir(repo)

    hook_pre_commit()

    assert "map/map.aprx" not in _staged_names(repo)
    assert any(n.startswith("map/map.aprx.src/") for n in _staged_names(repo))


# --------------------------------------------------------------------------- #
# pre-commit — simple mode (not mis-detected as env)
# --------------------------------------------------------------------------- #

def test_simple_precommit_packs_and_stages_binary(repo, monkeypatch):
    # AC: a simple-mode Project packs the staged source and stages the resulting
    # binary — and is NOT mis-detected as env-managed (env Projects never stage a
    # binary, so the binary's presence in the index proves correct classification).
    p = _make_simple_project(repo, "doc")
    explode(str(p.aprx), str(p.proj / "doc.aprx.src"))   # produce committable source
    _git(repo, "add", "doc/doc.aprx", "doc/doc.aprx.src")
    monkeypatch.chdir(repo)

    hook_pre_commit()

    staged = _staged_names(repo)
    assert "doc/doc.aprx" in staged                       # binary committed in simple mode
    assert any(n.startswith("doc/doc.aprx.src/") for n in staged)


# --------------------------------------------------------------------------- #
# pre-commit — mixed monorepo (each Project by its own Mode in one run)
# --------------------------------------------------------------------------- #

def test_mixed_repo_handles_each_project_by_its_own_mode(repo, monkeypatch):
    env = _make_env_project(repo, "env")
    sim = _make_simple_project(repo, "sim")
    explode(str(sim.aprx), str(sim.proj / "sim.aprx.src"))
    _git(repo, "add", "sim/sim.aprx", "sim/sim.aprx.src")
    monkeypatch.chdir(repo)

    hook_pre_commit()

    staged = _staged_names(repo)
    # env Project: tokenised source, no binary, no raw leak
    assert any(n.startswith("env/env.aprx.src/") for n in staged)
    assert "env/env.aprx" not in staged
    assert not _staged_contains(repo, env.value)
    # simple Project: binary staged
    assert "sim/sim.aprx" in staged


# --------------------------------------------------------------------------- #
# pre-push — returns the verify exit code
# --------------------------------------------------------------------------- #

def test_pre_push_passes_on_neutral_source(repo, monkeypatch):
    _make_env_project(repo, "map")
    monkeypatch.chdir(repo)
    assert hook_pre_push() == 0


def test_pre_push_blocks_on_leaked_raw_source(repo, monkeypatch):
    # Re-explode the working binary WITHOUT the env transform so a raw connection
    # string lands in source — verify (and so pre-push) must fail.
    p = _make_env_project(repo, "map")
    explode(str(p.aprx), str(p.src))                     # bare → leaks raw value
    monkeypatch.chdir(repo)
    assert hook_pre_push() != 0


# --------------------------------------------------------------------------- #
# Robustness — undeclared/misconfigured Projects must not leak or crash
# --------------------------------------------------------------------------- #

def test_simple_precommit_works_on_the_first_commit(tmp_path, monkeypatch):
    # A brand-new repo has no HEAD; pre-commit's unstage must not crash on it (the old
    # `git reset HEAD <path>` aborts fatally on an unborn branch).
    fresh = tmp_path / "fresh"
    subprocess.run(["git", "init", str(fresh)], check=True, capture_output=True)
    _git(fresh, "config", "user.email", "t@example.com")
    _git(fresh, "config", "user.name", "Tester")
    p = _make_simple_project(fresh, "doc")
    explode(str(p.aprx), str(p.proj / "doc.aprx.src"))
    _git(fresh, "add", "doc/doc.aprx", "doc/doc.aprx.src")
    monkeypatch.chdir(fresh)

    hook_pre_commit()                                    # must not crash on unborn HEAD

    staged = _staged_names(fresh)
    assert "doc/doc.aprx" in staged
    assert any(n.startswith("doc/doc.aprx.src/") for n in staged)


def test_corrupt_aprx_json_blocks_commit_without_leaking(repo, monkeypatch):
    # An env Project whose aprx.json is unreadable (e.g. merge-conflict markers) must
    # NOT be bare-exploded as if simple — that would leak the raw connection string.
    # Strict ProjectConfig.load blocks the commit (ADR-0001) before anything is staged.
    p = _make_env_project(repo, "map")
    (p.proj / "aprx.json").write_text("{ this is not valid json")
    _git(repo, "add", "map/map.aprx")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit):
        hook_pre_commit()
    assert not _staged_contains(repo, p.value)           # raw value never reached source


def test_corrupt_aprx_json_blocks_source_only_commit(repo, monkeypatch):
    # Issue 0004: the merge-resolution path — a developer edits Source inside the env
    # Project and stages the Source only (never the binary). With aprx.json corrupt the
    # Project is undeclared, so this path must block too. The Source-only twin of the
    # binary-path block above.
    #
    # The load-bearing guard is that **no derived binary is staged**: a regression that
    # treated the undeclared Project as simple would IDENTITY-pack the staged Source and
    # stage `map/map.aprx` here — exactly the committed-binary leak ADR-0001 forbids for an
    # undeclared Project. (A raw-string assertion would be vacuous on this path: the block
    # aborts before any pack, and IDENTITY-packing tokenised Source re-materialises no raw
    # value anyway.) Staged Source the developer added by hand is theirs; blocking the
    # commit means none of it is ever committed.
    p = _make_env_project(repo, "map")
    (p.proj / "aprx.json").write_text("{ this is not valid json")
    _git(repo, "add", "map/map.aprx.src")                # Source only — binary unstaged
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit):
        hook_pre_commit()

    assert "map/map.aprx" not in _staged_names(repo)     # no derived binary staged → no leak


def test_misconfigured_env_project_does_not_block_unrelated_commit(repo, monkeypatch, capsys):
    # An env Project declared mode:env but with no connections/*.json yet can't tokenise;
    # the env-refresh sweep runs on every commit, so it must skip that project (with a
    # hint) rather than abort an unrelated commit with a traceback.
    _make_env_project(repo, "map")
    for f in (repo / "map" / "connections").glob("*.json"):
        f.unlink()
    (repo / "map" / "local.json").unlink()
    (repo / "notes.txt").write_text("unrelated change")
    _git(repo, "add", "notes.txt")
    monkeypatch.chdir(repo)

    hook_pre_commit()                                    # must not raise

    assert "skipping" in capsys.readouterr().err
    assert "notes.txt" in _staged_names(repo)            # the real change still committed
