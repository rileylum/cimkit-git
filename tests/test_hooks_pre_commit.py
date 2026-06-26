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


def _make_undeclared_project(repo: Path, name: str) -> SimpleNamespace:
    """A Project with **no** aprx.json — its Mode is undeclared, so the pre-commit hook
    must block it (ADR-0001) rather than guess simple or env. The binary and a source
    tree exist (a developer who never ran `aprx install`)."""
    proj = repo / name
    proj.mkdir()
    aprx = proj / f"{name}.aprx"
    shutil.copy(SIMPLE_APRX, aprx)
    src = explode(str(aprx), str(proj / f"{name}.aprx.src"))
    return SimpleNamespace(proj=proj, aprx=aprx, src=Path(src))


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


def test_env_precommit_preserves_hand_edited_staged_source(repo, monkeypatch):
    # Issue 0007 — the merge-resolution path. A developer resolves a conflict by hand-editing
    # the tokenised Source inside an env Project and stages the Source ONLY (never the binary —
    # the env workflow). The working .aprx is stale relative to that edit: a conflicted merge
    # never fires post-merge, so build_working_copies never rebuilt the binary from the merged
    # Source. pre-commit's env-refresh must NOT re-explode the stale binary over the staged
    # Source — that would silently destroy the resolution and commit source re-derived from the
    # stale binary instead. The developer's staged edit must be what gets committed.
    p = _make_env_project(repo, "map")
    monkeypatch.chdir(repo)

    # The hand resolution: an edit present in the staged Source but NOT in the working binary
    # (which still holds only the original explode). If the refresh re-explodes the binary,
    # this marker disappears — exactly the silent loss 0007 guards.
    marker = "RESOLVED-BY-HAND-0007"
    gis = p.src / "GISProject.json"
    data = json.loads(gis.read_text())
    data["__resolved_marker__"] = marker
    gis.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    _git(repo, "add", "map/map.aprx.src")            # Source only — binary never staged

    hook_pre_commit()

    staged = _staged_names(repo)
    assert _staged_contains(repo, marker)            # the resolution survived — not clobbered
    # The 0004/leak + binary-lifecycle guarantees still hold on this path:
    assert not _staged_contains(repo, p.value)       # no raw connection string in staged source
    assert "map/map.aprx" not in staged              # the env binary is still never committed
    assert any(n.startswith("map/map.aprx.src/") for n in staged)


def test_env_precommit_retokenizes_raw_value_left_in_staged_source(repo, monkeypatch):
    # Issue 0007 leak guarantee: preserving the developer's staged Source must not weaken the
    # neutrality the old from-binary refresh enforced. If a hand resolution leaves a *raw*
    # connection string (registered in a committed environment) in the staged Source, the
    # in-place re-tokenise must replace it with its @@token@@ before commit — the raw value
    # must never reach the index. (A naive "just trust the staged Source" fix would commit it.)
    p = _make_env_project(repo, "map")
    monkeypatch.chdir(repo)

    # Plant raw connection strings into the Source as a botched resolution would: a *bare*
    # explode of the working binary (no tokenising transform) writes the raw value into the
    # Source, correctly JSON-escaped. Stage that.
    explode(str(p.aprx), str(p.src))
    assert not any("@@main@@" in f.read_text() for f in p.src.rglob("*.json"))  # raw, not neutral
    _git(repo, "add", "map/map.aprx.src")

    hook_pre_commit()

    assert _staged_contains(repo, "@@main@@")         # re-tokenised back to the placeholder
    assert not _staged_contains(repo, p.value)        # the raw value never reached the index
    assert "map/map.aprx" not in _staged_names(repo)  # binary still never committed


def test_env_precommit_preserves_staged_source_deletion(repo, monkeypatch):
    # Issue 0007 (the delete path): a developer stages the *removal* of a Source entry during a
    # merge resolution. The from-binary refresh would resurrect it (the working .aprx still
    # contains the entry); the in-place re-tokenise must leave the deletion intact. A pure
    # deletion is invisible to the ACM `staged` set — only the deletion-inclusive signal
    # diverts the dir to retokenize_env — so this is the exact gap a naive fix would miss.
    _make_env_project(repo, "map")
    monkeypatch.chdir(repo)
    _git(repo, "add", "map/map.aprx.src")
    _git(repo, "commit", "-m", "seed neutral source")  # track a real binary-derived entry

    victim = "map/map.aprx.src/map/test_table.json"    # present in Source AND the working .aprx
    assert (repo / victim).exists()
    _git(repo, "rm", victim)                            # stage the deletion only (no ACM change)

    hook_pre_commit()

    assert not (repo / victim).exists()                # not resurrected from the stale binary
    deleted = _git(repo, "diff", "--cached", "--name-only", "--diff-filter=D").stdout.split()
    assert "map/map.aprx.src/map/test_table.json" in deleted   # the deletion is what's committed


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
# pre-commit — simple mode tolerates a git-ignored binary (issue 0002)
# --------------------------------------------------------------------------- #

def test_simple_precommit_tolerates_untracked_ignored_binary(repo, monkeypatch):
    # AC: a simple-mode commit succeeds when the binary is untracked + git-ignored — the
    # hook must NOT abort. The author has hand-added the binary to .gitignore and never
    # tracked it; `git add` of that path exits non-zero, which the old check=True staging
    # turned into a whole-commit abort.
    p = _make_simple_project(repo, "doc")
    explode(str(p.aprx), str(p.proj / "doc.aprx.src"))
    (repo / ".gitignore").write_text("doc/doc.aprx\n")
    p.aprx.unlink()                                       # prove pack regenerates it below
    _git(repo, "add", "doc/doc.aprx.src", ".gitignore")  # source only; binary stays untracked
    monkeypatch.chdir(repo)

    hook_pre_commit()                                    # must not raise

    staged = _staged_names(repo)
    assert "doc/doc.aprx" not in staged                  # the ignored binary is not staged
    assert p.aprx.exists()                               # but pack still regenerated the working binary
    assert any(n.startswith("doc/doc.aprx.src/") for n in staged)  # source committed as normal


def test_simple_precommit_restages_tracked_but_ignored_binary(repo, monkeypatch):
    # AC: a tracked-but-ignored binary is still re-staged — git allows `git add` on a
    # tracked file regardless of .gitignore, so a half-migrated Project (ignored, not yet
    # `git rm --cached`) keeps its binary in sync. We stay agnostic and let git decide.
    p = _make_simple_project(repo, "doc")
    explode(str(p.aprx), str(p.proj / "doc.aprx.src"))
    _git(repo, "add", "doc/doc.aprx", "doc/doc.aprx.src")
    _git(repo, "commit", "-m", "seed tracked binary")    # binary is now tracked
    (repo / ".gitignore").write_text("doc/doc.aprx\n")    # ...and now also ignored

    # A source edit so pack re-derives a *different* binary — a re-stage then shows up in
    # the index (an unchanged blob would not appear in diff --cached).
    gis = p.proj / "doc.aprx.src" / "GISProject.json"
    data = json.loads(gis.read_text())
    data["__edit_0002__"] = "x"
    gis.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    _git(repo, "add", "doc/doc.aprx.src")
    monkeypatch.chdir(repo)

    hook_pre_commit()

    assert "doc/doc.aprx" in _staged_names(repo)          # tracked+ignored binary re-staged


def test_stage_packed_binary_reraises_non_ignore_failure(repo):
    # The tolerance is scoped to git's ignore-rejection only: a `git add` that fails for any
    # *other* reason (here a path that does not exist and is not ignored) must still propagate
    # exactly as the old check=True staging did — never be silently swallowed.
    from aprx_tools.hooks import _stage_packed_binary

    with pytest.raises(subprocess.CalledProcessError):
        _stage_packed_binary(repo, "does/not/exist.aprx")


def test_stage_packed_binary_reraises_failure_on_ignored_path(repo):
    # The sharp edge: a path that IS git-ignored but whose `git add` fails for a non-ignore
    # reason (here it does not exist → git's fatal exit 128, distinct from the ignore-refusal's
    # exit 1) must still propagate. Keying tolerance on `check-ignore` alone would misread this
    # as the benign ignore case and swallow a real error — the regression this guards.
    (repo / ".gitignore").write_text("ghost.aprx\n")
    from aprx_tools.hooks import _stage_packed_binary

    with pytest.raises(subprocess.CalledProcessError):
        _stage_packed_binary(repo, "ghost.aprx")        # ignored AND nonexistent


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


def test_unreadable_aprx_json_blocks_binary_commit_without_leaking(repo, deny_reading, monkeypatch):
    # Issue 0005: an unreadable aprx.json (permissions/IO, not corrupt content) on the
    # leak-sensitive staged-binary path must block the commit — never bare-explode as if
    # simple — exactly as the malformed-content twin does. The whole-repo crash an uncaught
    # OSError would cause is downgraded to this per-Project block.
    p = _make_env_project(repo, "map")
    _git(repo, "add", "map/map.aprx")
    deny_reading(p.proj / "aprx.json")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as exc:
        hook_pre_commit()

    assert "could not be read" in str(exc.value)         # the IO-specific diagnostic
    # The real leak guard: the block aborts *before* any bare-explode, so no Source was
    # derived and staged for the Project. (Asserting on the raw value alone is vacuous
    # here — on this path it lives only compressed inside the staged .aprx zip, where
    # `git grep --cached` can't see it; a regression treating undeclared-as-simple would
    # explode the binary and `git add` the source dir, which this catches.)
    assert not any(n.startswith("map/map.aprx.src/") for n in _staged_names(repo))


def test_two_undeclared_projects_are_both_named_in_one_block(repo, monkeypatch):
    # Issue 0008: a single commit staging two undeclared Projects must name BOTH in one
    # run — each with its `aprx install` hint — instead of reporting only the
    # alphabetically-first and forcing a fix-and-recommit drip. The plan already collects
    # every offending Project; this guards that the reporting step no longer throws the
    # rest away.
    _make_undeclared_project(repo, "alpha")
    _make_undeclared_project(repo, "bravo")
    _git(repo, "add", "alpha/alpha.aprx", "bravo/bravo.aprx")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as exc:
        hook_pre_commit()

    msg = str(exc.value)
    assert "alpha" in msg and "bravo" in msg                 # both Projects named
    assert msg.count("aprx install") == 2                    # each carries its own hint
    # Nothing was staged for either Project — the block aborts before any explode/pack.
    assert not any(n.startswith("alpha/alpha.aprx.src/") for n in _staged_names(repo))
    assert not any(n.startswith("bravo/bravo.aprx.src/") for n in _staged_names(repo))


def test_undeclared_project_staged_both_ways_is_named_once(repo, monkeypatch):
    # Issue 0008: a Project staged as both a binary and a Source dir lands in `blocked`
    # twice (distinct rel-paths), but de-duplicating to the Project dir means it is named
    # exactly once — not the same install hint printed twice for one Project.
    _make_undeclared_project(repo, "alpha")
    _git(repo, "add", "alpha/alpha.aprx", "alpha/alpha.aprx.src")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as exc:
        hook_pre_commit()

    assert str(exc.value).count("aprx install") == 1         # one Project → one message


def test_single_undeclared_project_blocks_with_one_message(repo, monkeypatch):
    # Issue 0008 no-regression: the common single-Project case still aborts with exactly
    # one clear message naming that Project and its hint.
    _make_undeclared_project(repo, "solo")
    _git(repo, "add", "solo/solo.aprx")
    monkeypatch.chdir(repo)

    with pytest.raises(SystemExit) as exc:
        hook_pre_commit()

    msg = str(exc.value)
    assert "solo" in msg
    assert msg.count("aprx install") == 1


def test_unreadable_aprx_json_does_not_block_unrelated_commit(repo, deny_reading, monkeypatch):
    # Issue 0005, the fail-open twin: the env-refresh sweep classifies the Project with the
    # unreadable config as UNDECLARED (not env), so it is skipped rather than crashing the
    # hook — an unrelated staged change still commits. This is the property that turns a
    # whole-repo commit blocker into a per-Project skip.
    p = _make_env_project(repo, "map")
    (repo / "notes.txt").write_text("unrelated change")
    _git(repo, "add", "notes.txt")
    deny_reading(p.proj / "aprx.json")
    monkeypatch.chdir(repo)

    hook_pre_commit()                                    # must not raise

    assert "notes.txt" in _staged_names(repo)            # the real change still committed
    assert not _staged_contains(repo, p.value)           # and the env binary was not staged


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


def test_unreadable_connections_file_does_not_block_unrelated_commit(repo, deny_reading, monkeypatch, capsys):
    # Issue 0009, one read site over from 0005's aprx.json fix: an env Project whose
    # connections/dev.json is present but unreadable makes the sweep's load_connections
    # (via explode_transform → committed_reverse_map) raise OSError. That now converts to a
    # SystemExit the sweep already catches, so the Project is skipped with a hint and an
    # unrelated change still commits — instead of an uncaught traceback crashing the whole
    # commit. deny_reading is applied after _make_env_project so its setup explode succeeds.
    p = _make_env_project(repo, "map")
    (repo / "notes.txt").write_text("unrelated change")
    _git(repo, "add", "notes.txt")
    deny_reading(p.proj / "connections" / "dev.json")
    monkeypatch.chdir(repo)

    hook_pre_commit()                                    # must not raise

    assert "skipping" in capsys.readouterr().err
    assert "notes.txt" in _staged_names(repo)            # the real change still committed
    assert not _staged_contains(repo, p.value)           # no bare-explode → raw value never staged
    assert not any(n.startswith("map/map.aprx.src/") for n in _staged_names(repo))


def test_malformed_connections_file_does_not_block_unrelated_commit(repo, monkeypatch, capsys):
    # The malformed twin of the above: a hand-broken / merge-conflicted connections/dev.json
    # raises JSONDecodeError in load_connections, which (pre-0009) the sweep did NOT catch.
    # Converted to a SystemExit, it is skipped like any unresolvable env Project, so an
    # unrelated change still commits and no raw value leaks via a bare explode.
    p = _make_env_project(repo, "map")
    (p.proj / "connections" / "dev.json").write_text("{ not valid json")
    (repo / "notes.txt").write_text("unrelated change")
    _git(repo, "add", "notes.txt")
    monkeypatch.chdir(repo)

    hook_pre_commit()                                    # must not raise

    assert "skipping" in capsys.readouterr().err
    assert "notes.txt" in _staged_names(repo)
    assert not _staged_contains(repo, p.value)
    assert not any(n.startswith("map/map.aprx.src/") for n in _staged_names(repo))
