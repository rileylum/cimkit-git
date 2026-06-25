"""Plan-level unit tests for the pre-commit *decide* step (issue 0001).

`plan_precommit` is pure: given staged rel-paths, the repo's `.aprx.src` dirs, and a
`classify` callable, it returns a `StagePlan` value touching no git and no filesystem.
That lets the leak rules the hook exists to enforce be asserted on the plan directly,
with **no `git init`** and no throwaway repo:

  * an environment-mode Project's binary never appears in any stage action;
  * an undeclared Project yields a *block* outcome, never a bare-explode;
  * a Source dir nested in a monorepo subdirectory attributes to the right Project.

The real-repo `apply_plan` side stays covered by `test_hooks_pre_commit.py`.
"""

from pathlib import Path

from aprx_tools.hooks import ENV, SIMPLE, UNDECLARED, StagePlan, plan_precommit

ROOT = Path("/repo")


def _classify_from(modes: dict):
    """A pure `classify`: look a Project dir up in *modes*, defaulting to UNDECLARED
    (no readable `aprx.json`) — the stand-in for the dict the issue says tests pass."""
    return lambda project_dir: modes.get(project_dir, UNDECLARED)


def _src(*parts) -> Path:
    return ROOT.joinpath(*parts)


# --------------------------------------------------------------------------- #
# Purity
# --------------------------------------------------------------------------- #

def test_plan_is_pure_value_with_no_io(tmp_path, monkeypatch):
    # No git, no filesystem: point cwd at an empty dir and forbid subprocess — a pure
    # function can't notice.
    def _boom(*a, **k):
        raise AssertionError("plan_precommit must not shell out")

    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr("subprocess.run", _boom)
    monkeypatch.setattr("subprocess.check_output", _boom)

    plan = plan_precommit(
        ROOT,
        {"map/map.aprx"},
        [_src("map", "map.aprx.src")],
        _classify_from({_src("map"): SIMPLE}),
    )
    assert isinstance(plan, StagePlan)


# --------------------------------------------------------------------------- #
# Environment mode — the binary never appears in a stage action
# --------------------------------------------------------------------------- #

def test_env_source_is_refreshed_binary_never_staged():
    src = _src("map", "map.aprx.src")
    plan = plan_precommit(
        ROOT, set(), [src], _classify_from({_src("map"): ENV})
    )
    assert plan.refresh_env == (src,)
    # The env binary is in no stage action: not exploded, not packed.
    assert plan.explode_simple == ()
    assert plan.pack == ()
    assert plan.blocked == ()


def test_env_staged_binary_is_unstaged_not_exploded():
    # Even if an env Project's binary is staged, it is dropped — never turned into
    # committed source (its neutral source is refreshed instead).
    src = _src("map", "map.aprx.src")
    plan = plan_precommit(
        ROOT, {"map/map.aprx"}, [src], _classify_from({_src("map"): ENV})
    )
    assert plan.unstage == ("map/map.aprx",)
    assert plan.explode_simple == ()
    assert plan.pack == ()


def test_staged_source_inside_env_dir_retokenizes_and_never_touches_the_binary():
    # Issue 0007: staging Source by hand inside an env dir routes it to retokenize_env, NOT
    # refresh_env — the developer's staged Source is preserved (re-tokenised in place), never
    # re-derived from the possibly-stale working binary. And the env binary is in NO stage
    # action: not packed, not exploded, not unstaged — so neither env path can commit it.
    src = _src("map", "map.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"map/map.aprx.src/GISProject.json"},
        [src],
        _classify_from({_src("map"): ENV}),
    )
    assert plan.retokenize_env == (src,)  # staged Source → re-tokenise in place...
    assert plan.refresh_env == ()         # ...not re-derived from the binary
    assert plan.pack == ()                # binary never committed by the simple pass
    assert plan.explode_simple == ()
    assert plan.unstage == ()


def test_env_source_with_no_staged_source_is_refreshed_from_binary():
    # Issue 0007 only diverts to retokenize_env when the developer staged Source. The normal
    # env workflow (developer edits the binary, stages nothing in the `.src/`) must still
    # refresh from the binary: an unrelated staged file does not count as staging this
    # Project's Source.
    src = _src("map", "map.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"notes.txt"},                    # staged, but not inside the env Source dir
        [src],
        _classify_from({_src("map"): ENV}),
    )
    assert plan.refresh_env == (src,)     # no staged Source here → refresh runs as before
    assert plan.retokenize_env == ()


def test_staged_env_source_deletion_diverts_to_retokenize():
    # Issue 0007 (the delete path): a developer stages the *removal* of a Source entry as
    # part of a merge resolution and nothing else. That deletion arrives only via the
    # deletion-inclusive set (the ACM `staged` omits it), and it must still divert the dir to
    # retokenize_env — otherwise the from-binary refresh would resurrect the deleted entry.
    src = _src("map", "map.aprx.src")
    plan = plan_precommit(
        ROOT,
        set(),                                        # ACM: nothing (a pure deletion)
        [src],
        _classify_from({_src("map"): ENV}),
        staged_incl_deletions={"map/map.aprx.src/cimlayers/Old.json"},
    )
    assert plan.retokenize_env == (src,)  # the staged deletion is honoured...
    assert plan.refresh_env == ()         # ...not undone by a re-explode of the binary


# --------------------------------------------------------------------------- #
# Simple mode — explode the binary, re-derive a normalised one
# --------------------------------------------------------------------------- #

def test_simple_staged_binary_explodes_and_packs():
    plan = plan_precommit(
        ROOT, {"doc/doc.aprx"}, [], _classify_from({_src("doc"): SIMPLE})
    )
    assert plan.explode_simple == (_src("doc", "doc.aprx"),)
    assert plan.unstage == ("doc/doc.aprx",)          # dropped, then re-added normalised
    assert plan.pack == (_src("doc", "doc.aprx.src"),)
    assert plan.blocked == ()


def test_simple_staged_binary_and_its_source_pack_once():
    # The binary and its already-exploded Source are both staged (the common case); the
    # Source dir must be packed exactly once, not duplicated.
    src = _src("doc", "doc.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"doc/doc.aprx", "doc/doc.aprx.src/GISProject.json"},
        [src],
        _classify_from({_src("doc"): SIMPLE}),
    )
    assert plan.pack == (src,)


def test_staged_source_edit_packs_without_binary_staged():
    # Merge-conflict resolution / direct Source edit: only Source files are staged, yet
    # the binary is re-derived.
    src = _src("doc", "doc.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"doc/doc.aprx.src/GISProject.json"},
        [src],
        _classify_from({_src("doc"): SIMPLE}),
    )
    assert plan.pack == (src,)
    assert plan.explode_simple == ()


# --------------------------------------------------------------------------- #
# Undeclared mode — block, never bare-explode
# --------------------------------------------------------------------------- #

def test_undeclared_staged_binary_blocks_and_never_explodes():
    plan = plan_precommit(
        ROOT, {"x/x.aprx"}, [], _classify_from({})  # no declaration for x/
    )
    assert plan.blocked == ("x/x.aprx",)
    # The block outcome, not a bare-explode that would leak a raw connection string.
    assert plan.explode_simple == ()
    assert plan.pack == ()
    assert plan.unstage == ()


def test_undeclared_source_only_blocks_and_never_packs():
    # Issue 0004: a merge resolved by editing Source inside an undeclared Project — only
    # Source files staged, no binary — must block too, not be packed with IDENTITY into a
    # binary of unsubstituted tokens / raw values. The Source-only twin of the binary test.
    src = _src("x", "x.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"x/x.aprx.src/GISProject.json"},
        [src],
        _classify_from({}),                          # no declaration for x/
    )
    assert plan.blocked == ("x/x.aprx.src",)         # parent is the Project dir for the re-raise
    assert plan.pack == ()
    assert plan.explode_simple == ()
    assert plan.unstage == ()


def test_undeclared_many_source_files_block_as_one_entry():
    # Several staged Source files in one undeclared Project collapse to a single Source-dir
    # block entry (not one per file). A staged binary for the same Project is a *distinct*
    # entry and is kept — both rel-paths share the Project dir as parent, so either drives
    # the same abort in apply_plan; the binary/Source pair is not de-duplicated to one.
    src = _src("x", "x.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"x/x.aprx", "x/x.aprx.src/a.json", "x/x.aprx.src/b.json"},
        [src],
        _classify_from({}),
    )
    assert plan.blocked == ("x/x.aprx", "x/x.aprx.src")   # files → one entry; binary distinct
    assert plan.pack == ()
    assert plan.explode_simple == ()


# --------------------------------------------------------------------------- #
# Nested monorepo — attribute each Source dir to the right Project
# --------------------------------------------------------------------------- #

def test_nested_source_attributes_to_its_own_project():
    # A Source dir several directories deep is found, and classified by *its* parent —
    # not the repo root.
    seen = []

    def classify(project_dir):
        seen.append(project_dir)
        return SIMPLE

    nested = _src("teams", "gis", "atlas.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"teams/gis/atlas.aprx.src/cimmaps/Map.json"},
        [nested],
        classify,
    )
    assert plan.pack == (nested,)
    assert _src("teams", "gis") in seen          # classified by its own Project dir


def test_two_nested_projects_pack_independently():
    a = _src("a", "one.aprx.src")
    b = _src("b", "deep", "two.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"a/one.aprx.src/GISProject.json", "b/deep/two.aprx.src/GISProject.json"},
        [a, b],
        _classify_from({_src("a"): SIMPLE, _src("b", "deep"): SIMPLE}),
    )
    assert plan.pack == (a, b)


# --------------------------------------------------------------------------- #
# Mixed monorepo — each Project handled by its own Mode in one plan
# --------------------------------------------------------------------------- #

def test_mixed_repo_handles_each_project_by_its_mode():
    env_src = _src("env", "env.aprx.src")
    sim_src = _src("sim", "sim.aprx.src")
    plan = plan_precommit(
        ROOT,
        {"sim/sim.aprx", "sim/sim.aprx.src/GISProject.json"},
        [env_src, sim_src],
        _classify_from({_src("env"): ENV, _src("sim"): SIMPLE}),
    )
    # env: refreshed, never staged as a binary
    assert plan.refresh_env == (env_src,)
    assert _src("env", "env.aprx") not in plan.explode_simple
    assert env_src not in plan.pack
    # simple: exploded and re-packed
    assert plan.explode_simple == (_src("sim", "sim.aprx"),)
    assert plan.pack == (sim_src,)
    assert plan.unstage == ("sim/sim.aprx",)
