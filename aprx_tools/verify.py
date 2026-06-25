"""`aprx verify` — a single exit-coded gate for CI.

The check that every CI system calls. It does NOT need GitHub — it is a plain
command that exits non-zero on failure, so GitHub Actions, GitLab CI, Azure
Pipelines, or any runner can invoke it identically.

The Mode decision comes from the authoritative committed ``aprx.json`` via
``ProjectConfig`` (ADR-0001), never from the old presence-sniffing heuristic — so
CI checks exactly the Mode the team declared. A Project that declares no Mode fails
with the "run ``aprx install``" guidance instead of silently passing; the failure is
collected like any other so the repo-wide gate still reports every project.

For an environment-mode Project it asserts:
  * the committed source is fully tokenised (no raw connection string leaked in —
    i.e. nobody committed without the hooks), and
  * every token the source references has a value in every environment file (the
    project actually builds for each environment).

For a simple-mode Project it asserts the committed .aprx is present (a simple-mode
Project commits both Source and binary) and in sync with a fresh pack of its source.
"""

import sys
import tempfile
from pathlib import Path

from .entry import parsed_json_entries
from .project_config import ProjectConfig
from .util import aprx_for_src_dir, git_root, iter_src_dirs
from .pack import pack
from .compare import compare


def _verify_env_project(src_dir: Path, cfg: ProjectConfig, env: str, problems: list) -> None:
    # parsed_json_entries (issue 0002) is the read-only **skip** policy: it yields only
    # parseable JSON, silently dropping anything else. Materialise it once into a list:
    # ProjectConfig answers two questions over the same Source below (the leak check and
    # the per-env coverage check), and a spent generator would answer the second empty.
    # The domain questions themselves — is the Source neutral, does each env cover its
    # keys — now live on ProjectConfig (issue 0003), so verify no longer hand-assembles
    # them from `scan_tokens` + set math.
    parsed = [entry.parsed for entry in parsed_json_entries(src_dir)]

    raw = cfg.leaked_values(parsed)
    if raw:
        problems.append(
            f"{src_dir.name}: raw connection string(s) in source — committed without "
            f"hooks?\n      " + "\n      ".join(sorted(raw))
        )

    # The committed, team-shared environments — discovered by the one shared rule
    # (`committed_connection_files`) so explode tokenises against and verify checks
    # against the identical file set, never a hand-rolled `connections/<env>.json`
    # copy (issue 0004). `--env` just narrows that same set by file stem.
    env_files = cfg.committed_connection_files()
    if env:
        env_files = [f for f in env_files if f.stem == env]
        if not env_files:
            problems.append(f"{src_dir.name}: no connections file for env {env!r}")
            return
    elif not env_files:
        problems.append(
            f"{src_dir.name}: no connections/*.json to verify against — "
            f"run `aprx connections init` or add a connections file"
        )
        return

    for env_file in env_files:
        missing = cfg.unresolved_keys(parsed, env_file)
        if missing:
            problems.append(
                f"{src_dir.name}: {env_file.name} missing keys: " + ", ".join(sorted(missing))
            )


def _verify_simple_project(src_dir: Path, problems: list) -> None:
    aprx = aprx_for_src_dir(src_dir)  # util owns the src↔binary naming convention
    if not aprx.exists():
        # A simple-mode Project commits both the Source and the binary; the .aprx is
        # the committed artifact, the Source its diffable rendering (CLAUDE.md "What
        # this is"). Source present + binary absent is an incomplete commit, not a
        # valid state, so the in-sync gate (PRD story 20) has nothing to check and
        # the repo cannot be rebuilt — report it rather than passing silently. (Env
        # mode's missing .aprx is a gitignored build artifact and never reaches here:
        # this runs only on verify()'s `else` branch, ADR-0001 / issue 0007.)
        problems.append(
            f"{src_dir.name}: committed {aprx.name} is missing — pack the source and "
            f"commit it (run the hooks, or `aprx pack {src_dir.name}`)"
        )
        return
    with tempfile.TemporaryDirectory() as tmp:
        rebuilt = pack(str(src_dir), str(Path(tmp) / aprx.name))
        if compare(str(aprx), str(rebuilt)):
            problems.append(
                f"{src_dir.name}: committed {aprx.name} is out of sync with its source "
                f"(committed without hooks?)"
            )


def verify(src_dir: str = None, env: str = None) -> int:
    if src_dir is not None:
        targets = [Path(src_dir)]
    else:
        targets = list(iter_src_dirs(git_root(required=False)))

    if not targets:
        print("aprx verify: no .aprx.src directories found", file=sys.stderr)
        return 1

    problems: list = []
    for sd in targets:
        # Strict resolution (ADR-0001): the Mode is read from the committed `aprx.json`
        # adjacent to the source, not guessed. A Project with no declared Mode is a
        # failure carrying the "run `aprx install`" guidance — but as the single
        # repo-wide CI gate, verify must check *every* project and report all of them,
        # so an un-migrated project becomes one collected problem rather than a
        # `ProjectConfig.load` hard-exit that aborts the loop and masks its siblings.
        try:
            cfg = ProjectConfig.load(sd.parent)
        except SystemExit as e:
            problems.append(f"{sd.name}: {e.code}")
            continue
        if cfg.is_env:
            _verify_env_project(sd, cfg, env, problems)
        else:
            _verify_simple_project(sd, problems)

    if problems:
        print(f"aprx verify: FAILED ({len(problems)} problem(s))", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"aprx verify: OK ({len(targets)} project(s) checked)")
    return 0
