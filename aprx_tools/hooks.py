"""Logic executed by the installed git hooks."""

import json
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from .explode import explode
from .pack import pack
from .project_config import ENV, SIMPLE, ProjectConfig
from .transform import SubstitutionError, explode_transform, pack_transform
from .util import (
    aprx_for_src_dir,
    aprx_output_for,
    git_root,
    iter_src_dirs,
    src_dir_for,
)
from . import connections as conn

#: A Project whose ``aprx.json`` cannot be read (missing / malformed / no ``mode``).
#: Distinct from ``SIMPLE``/``ENV`` so :func:`plan_precommit` can apply the strict rule
#: on the leak-sensitive path (block the commit) while the fail-open sweep just skips it.
UNDECLARED = "undeclared"


def _git(root: Path, *args) -> str:
    return subprocess.check_output(["git"] + list(args), cwd=root, text=True).strip()


def _git_run(root: Path, *args) -> None:
    subprocess.run(["git"] + list(args), cwd=root, check=True, capture_output=True)


def _staged(root: Path) -> set:
    output = _git(root, "diff", "--cached", "--name-only", "--diff-filter=ACM")
    return set(output.splitlines()) if output else set()


def _has_head(root: Path) -> bool:
    """True once the repo has at least one commit (``HEAD`` resolves)."""
    return subprocess.run(
        ["git", "rev-parse", "--verify", "--quiet", "HEAD"],
        cwd=root, capture_output=True,
    ).returncode == 0


def _unstage(root: Path, rel: str) -> None:
    """Drop *rel* from the index. ``git reset HEAD <path>`` needs a ``HEAD`` to reset
    against and fails fatally on the very first commit (no ``HEAD`` yet, so the whole
    hook would abort); there the file can only be newly-added, so ``git rm --cached``
    removes the index entry without touching the work tree."""
    if _has_head(root):
        _git_run(root, "reset", "HEAD", rel)
    else:
        _git_run(root, "rm", "--cached", "--quiet", rel)


def _classify_project(project_dir: Path) -> str:
    """Read a Project's declared **Mode** from its committed ``aprx.json`` (ADR-0001):
    ``ENV``, ``SIMPLE``, or ``UNDECLARED`` when the declaration can't be read.

    Mode is never sniffed from stray files — since every Project (simple ones too) now
    carries an ``aprx.json``, presence-sniffing would mis-classify a simple Project as
    env-managed and never stage its binary. The three-valued answer lets one lookup
    serve both policies the plan needs: the fail-open sweep treats ``UNDECLARED`` as
    "not env, leave it alone", while the leak-sensitive staged-binary path treats it as
    a hard block (so an undeclared Project is never bare-exploded as if it were simple —
    exactly the raw-connection-string leak we guard)."""
    try:
        return ENV if ProjectConfig.load(project_dir).is_env else SIMPLE
    except SystemExit:
        return UNDECLARED


def _containing_src_dir(src_dirs: "set[Path]", abs_path: Path) -> "Path | None":
    """The known ``.aprx.src`` directory in *src_dirs* that contains *abs_path* (so a
    Project nested in a monorepo subdirectory is found, not just one at the repo root),
    or ``None``. Pure path math against the precomputed set — no filesystem probing — so
    it is safe to call from :func:`plan_precommit`."""
    for ancestor in (abs_path, *abs_path.parents):
        if ancestor in src_dirs:
            return ancestor
    return None


# --------------------------------------------------------------------------- #
# pre-commit — decide (a pure plan) then do (the only index-touching code)
# --------------------------------------------------------------------------- #

@dataclass(frozen=True)
class StagePlan:
    """What the pre-commit hook must do, expressed as data (issue 0001).

    Computed by :func:`plan_precommit` with no git and no filesystem writes, then
    executed by :func:`apply_plan`. Splitting *decide* from *do* makes the leak rules —
    an env Project's binary is never staged; an undeclared Project blocks rather than
    bare-explodes; nested Source dirs attribute to the right Project — assertable on the
    value alone, with no throwaway git repo.

    Attributes:
        refresh_env:    env-mode Source dirs to re-explode (tokenised) and stage; their
                        binary is never committed. Listed for *every* env Project,
                        whether or not the refresh later succeeds, so a misconfigured
                        one is still excluded from ``pack``.
        explode_simple: staged simple-mode binaries (abs ``.aprx``) to explode faithfully
                        (``IDENTITY``) into Source and stage.
        unstage:        staged binary rel-paths to drop from the index — env binaries
                        (never committed) and the just-exploded simple binaries (re-added
                        normalised by ``pack``).
        pack:           Source dirs to pack and stage the resulting binary — the
                        simple-mode workflow plus merge-conflict resolution.
        blocked:        rel-paths whose Project has no declared Mode — a staged binary, or
                        a Source dir whose files were staged without the binary (issue
                        0004). Either form's parent is the Project dir, so ``apply_plan``
                        can re-raise the strict ``ProjectConfig.load`` error against it.
                        Their presence aborts the whole commit (no bare-explode/pack leak).
    """

    refresh_env: tuple = ()
    explode_simple: tuple = ()
    unstage: tuple = ()
    pack: tuple = ()
    blocked: tuple = ()


def plan_precommit(root: Path, staged, src_dirs, classify) -> StagePlan:
    """Decide the pre-commit actions as a pure value — no git, no filesystem.

    Args:
        root:     the repository top-level, used only for path arithmetic.
        staged:   rel-paths of the staged (added/copied/modified) files.
        src_dirs: every ``.aprx.src`` directory in the repo (absolute), discovered by
                  the caller — passed in so this function reads nothing itself.
        classify: ``(project_dir) -> ENV | SIMPLE | UNDECLARED``. Real callers pass
                  :func:`_classify_project`; tests pass a plain dict lookup.
    """
    src_dir_set = set(src_dirs)

    # Env-mode Source dirs: re-explode the working binary to neutral source, stage the
    # source only. Collected regardless of later success so a misconfigured env Project
    # stays out of the simple ``pack`` pass (its binary must never be committed).
    refresh_env = sorted(
        (sd for sd in src_dir_set if classify(sd.parent) == ENV), key=str
    )

    explode_simple: list = []
    unstage: list = []
    blocked: list = []
    pack: set = set()

    # Staged binaries. The Mode lookup here is leak-sensitive (a staged binary becomes
    # committed source), so an UNDECLARED Project blocks rather than bare-explodes.
    for rel in staged:
        if not rel.endswith(".aprx"):
            continue
        aprx_abs = root / rel
        mode = classify(aprx_abs.parent)
        if mode == UNDECLARED:
            blocked.append(rel)
        elif mode == ENV:
            # Neutral source already refreshed above; the binary is never committed.
            unstage.append(rel)
        else:
            # Simple: explode (IDENTITY is faithful), then re-derive a normalised binary
            # from that source so the committed .aprx is stable across machines.
            explode_simple.append(aprx_abs)
            unstage.append(rel)
            pack.add(src_dir_for(aprx_abs))

    # Staged Source files → decide by the containing Project's Mode — the same three-valued
    # classification the staged-binary loop uses, so an undeclared Project blocks on this
    # path too (issue 0004). A developer who resolves a merge by editing Source inside an
    # env/undeclared Project (staging Source, never the binary) must not slip past the
    # leak guard: SIMPLE packs (merge-conflict resolution + the simple-mode workflow), ENV
    # is excluded (its binary is built locally, never committed), and UNDECLARED blocks
    # rather than be packed with IDENTITY into a binary of unsubstituted tokens / raw values.
    for rel in staged:
        src_top = _containing_src_dir(src_dir_set, root / rel)
        if src_top is None:
            continue
        mode = classify(src_top.parent)
        if mode == UNDECLARED:
            blocked.append(str(src_top.relative_to(root)))
        elif mode == SIMPLE:
            pack.add(src_top)

    return StagePlan(
        refresh_env=tuple(refresh_env),
        explode_simple=tuple(sorted(explode_simple, key=str)),
        unstage=tuple(sorted(unstage)),
        pack=tuple(sorted(pack, key=str)),
        blocked=tuple(sorted(set(blocked))),
    )


def _refresh_env_source(root: Path, src_dir: Path) -> None:
    """Re-explode one env Project's working binary into **neutral** (tokenised) source
    and stage the source only — the binary is never committed.

    A misconfigured env project (no ``connections/*.json`` yet, or a connection string
    registered in none of them) must not abort the *whole* commit: this sweep runs over
    every project on every commit, so one bad project would block unrelated work and dump
    a traceback. Skipping never bare-explodes, so no raw string leaks; pre-push/`verify`
    is the gate that actually blocks until it's fixed. The env transform is built from the
    same composition-root helper the CLI dispatch uses, so the two roots can never drift."""
    aprx = aprx_for_src_dir(src_dir)
    if not aprx.exists():
        return
    try:
        explode(str(aprx), str(src_dir), transform=explode_transform(src_dir.parent))
    except (SystemExit, SubstitutionError) as e:
        print(f"  aprx-tools: skipping {src_dir.name} — {e}", file=sys.stderr)
        return
    _git_run(root, "add", str(src_dir.relative_to(root)))


def apply_plan(root: Path, plan: StagePlan) -> None:
    """Execute a :class:`StagePlan` against the git index — the **only** index-touching
    code in the pre-commit flow.

    A blocked Project aborts before anything is staged: re-raise the strict
    ``ProjectConfig.load`` error (ADR-0001, the ``aprx install`` hint) so the leak-
    sensitive path fails with its precise diagnostic and nothing is half-committed.
    apply_plan is the I/O side of the seam, so reproducing that exact wording here (the
    file says *why* — missing, malformed, or no ``mode``) is cheaper than threading every
    variant through the plan. The fallback fires only if the file became readable in the
    sub-millisecond window between decide and do (a single-process hook) — still abort,
    since the binary was undeclared when the plan was decided."""
    if plan.blocked:
        rel = plan.blocked[0]
        ProjectConfig.load((root / rel).parent)  # raises SystemExit
        sys.exit(f"aprx-tools: {rel}: Project mode could not be read — run `aprx install`")

    for src_dir in plan.refresh_env:
        _refresh_env_source(root, src_dir)

    for aprx_abs in plan.explode_simple:
        src_dir = src_dir_for(aprx_abs)
        explode(str(aprx_abs), str(src_dir))
        _git_run(root, "add", str(src_dir.relative_to(root)))

    for rel in plan.unstage:
        _unstage(root, rel)

    # Pack runs after unstage so a simple Project's just-unstaged binary is re-added
    # here, normalised — not dropped again.
    for src_dir in plan.pack:
        aprx_path = aprx_for_src_dir(src_dir)
        pack(str(src_dir), str(aprx_path))
        _git_run(root, "add", str(aprx_path.relative_to(root)))


def hook_pre_commit() -> None:
    root = git_root()
    # Memoise within this one invocation: a Project commonly appears both as a discovered
    # Source dir and as a staged binary, and ProjectConfig.load re-reads + re-parses
    # aprx.json on every call. A fresh dict per invocation (not a module-level cache)
    # keeps the read once-per-Project without going stale across hook runs.
    cache: dict = {}

    def classify(project_dir: Path) -> str:
        if project_dir not in cache:
            cache[project_dir] = _classify_project(project_dir)
        return cache[project_dir]

    plan = plan_precommit(root, _staged(root), list(iter_src_dirs(root)), classify)
    apply_plan(root, plan)


# --------------------------------------------------------------------------- #
# post-merge / post-checkout / post-stash — rebuild local working copies
# --------------------------------------------------------------------------- #

def build_working_copies(root: Path = None, src_dir: str = None, env: str = None) -> None:
    """Rebuild the working .aprx for one or all src dirs from the resolved
    connections (default local.json). Env-managed projects without a resolvable
    connections file are skipped with a hint rather than producing a binary full
    of unsubstituted tokens."""
    if src_dir is not None:
        targets = [Path(src_dir)]
    else:
        if root is None:
            root = git_root()
        targets = list(iter_src_dirs(root))

    for sd in targets:
        project_dir = sd.resolve().parent

        # Read the declared Mode (ADR-0001) — never sniffed. A Project with no
        # declaration (e.g. not yet `aprx install`ed) can't be resolved strictly, so
        # skip it: a never-blocking post-* hook reports and moves on rather than crashing.
        try:
            cfg = ProjectConfig.load(project_dir)
        except SystemExit as e:
            print(f"  aprx-tools: skipping {sd.name} — {e}", file=sys.stderr)
            continue

        # An env-mode Project with no resolvable connections file can't be built into a
        # working copy without emitting unsubstituted tokens — skip with a hint rather
        # than leak a broken binary full of @@tokens@@.
        if cfg.is_env and conn.resolve_connections_file(project_dir, env, None) is None:
            print(f"  aprx-tools: skipping {sd.name} — no {conn.LOCAL_FILE} "
                  f"(copy {conn.LOCAL_FILE}.example and fill in your connections)",
                  file=sys.stderr)
            continue

        # pack is connection-ignorant (ADR-0002); pack_transform carries the env
        # substitution (or IDENTITY for simple mode). These post-* hooks are documented
        # never to block, so a Project whose env is missing a key (SubstitutionError) or
        # otherwise won't resolve (sys.exit) downgrades to a skip, not a crashed hook.
        try:
            transform = pack_transform(project_dir, env=env)
            pack(str(sd), str(aprx_output_for(sd)), transform=transform)
        except (SystemExit, SubstitutionError, json.JSONDecodeError) as e:
            # JSONDecodeError covers a hand-broken connections/local.json (load_connections
            # parses it raw): these post-* hooks are documented never to block, so a
            # malformed file downgrades to a skip instead of crashing the rebuild.
            print(f"  aprx-tools: skipping {sd.name} — {e}", file=sys.stderr)


def hook_post_merge() -> None:
    build_working_copies(git_root())


def hook_post_checkout() -> None:
    build_working_copies(git_root())


def hook_post_stash() -> None:
    """Repack all .aprx.src directories after a stash pop so the local .aprx
    stays in sync with the checked-out src files."""
    build_working_copies(git_root())


def hook_pre_push() -> int:
    """Pre-push gate — the local mirror of the CI `aprx verify` check. Blocks a
    push whose source is untokenised or won't build for every environment.
    Returns the verify exit code so the hook can fail the push."""
    from .verify import verify
    return verify()
