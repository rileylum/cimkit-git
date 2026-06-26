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

For a simple-mode Project the binary check honours the declared ``commit_binary``
policy (issue 0003): with no declared policy (absent) or ``false`` it is
sync-if-present — a missing committed .aprx is OK (the Source is the canonical
truth, the binary a regenerated artifact) and a *present* .aprx is asserted in
sync with a fresh pack of its source; with ``commit_binary: true`` a missing binary
is a hard failure, restoring the incomplete-commit detection for teams that opt in.
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


def _verify_simple_project(src_dir: Path, cfg: ProjectConfig, problems: list) -> None:
    aprx = aprx_for_src_dir(src_dir)  # util owns the src↔binary naming convention
    if not aprx.exists():
        # Policy branch on the declared binary lifecycle (issue 0003). Only an explicit
        # `commit_binary: true` *requires* the binary; the tri-state collapses to "is it
        # truthy" exactly here — None (absent, no declared policy) and False (declared
        # no-commit) are both lenient, True alone is strict.
        if cfg.commit_binary:
            # The opt-in restores the incomplete-commit detection 0001 relaxed. Simple
            # mode can always rebuild a faithful, neutral binary, so under a declared
            # commit-it policy an absent binary is a forgotten commit, not a legitimate
            # Source-only repo.
            problems.append(
                f"{src_dir.name}: committed {aprx.name} is missing but commit_binary "
                f"is true (run `aprx pack` / the hooks and commit it)"
            )
            return
        # Sync-if-present (absent / `false` policy): a missing committed binary is OK.
        # By the tool's own principle the Source is the canonical truth and the .aprx is
        # a regenerated artifact (CLAUDE.md "What this is"), so "Source-only" — an author
        # who has hand-ignored the binary and committed only the diffable Source — is a
        # legitimate state, not an incomplete commit.
        #
        # Tradeoff knowingly dropped here: with no binary present and no opt-in, verify
        # cannot catch "author forgot to commit the binary"; `commit_binary: true` above
        # is exactly the knob that restores it. A *present* binary is still
        # packed-and-compared below, so a stale committed binary is still caught.
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
        # so an un-migrated project becomes one collected problem rather than a hard-exit
        # that aborts the loop and masks its siblings. The catch wraps the whole
        # per-project body, not just `ProjectConfig.load`: an unreadable / non-UTF-8 /
        # malformed connections file now hard-exits from `load_connections` too (issue
        # 0009), so it must be collected as one project's problem rather than abort the
        # repo-wide gate before its siblings are checked.
        try:
            cfg = ProjectConfig.load(sd.parent)
            if cfg.is_env:
                _verify_env_project(sd, cfg, env, problems)
            else:
                _verify_simple_project(sd, cfg, problems)
        except SystemExit as e:
            problems.append(f"{sd.name}: {e.code}")
            continue

    if problems:
        print(f"aprx verify: FAILED ({len(problems)} problem(s))", file=sys.stderr)
        for p in problems:
            print(f"  - {p}", file=sys.stderr)
        return 1

    print(f"aprx verify: OK ({len(targets)} project(s) checked)")
    return 0
