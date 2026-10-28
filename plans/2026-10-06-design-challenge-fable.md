<!-- model: claude-fable-5-1 (Claude subagent, read-only); reviewed docs/design.md draft against main @ b25c49e -->
# Adversarial review: cimkit-git DESIGN.md

Verdict: rethink. The sync model and leak guard need redesign before the surrounding
structure is worth building.

## 1. Critical: the leak guard only sees configured fields (Environments / Rules)

`tests/fixtures/simple/simple.aprx` has `projectItems[*].pathHint` set to a Windows user
path that includes the user and organisation names. It also has `propertiesXML` and
`settingsXML` strings that embed paths in XML. None of these is under
`workspaceConnectionString`.

- **Failure:** a user commits with the default config, and `check` passes. User names,
  the organisation name and the machine layout land in Source on every branch, which
  breaks goal 2.
- **The reverse problem:** `workspaceConnectionString = "DATABASE=.\simple.gdb"` is
  relative and environment-neutral, yet the guard demands it be registered.
- **Direction:**
  - Field rules need per-field `ignore` or `neutral` patterns.
  - Ship a default list of known machine-specific CIM fields.
  - `check` should scan every string value for absolute paths, UNC paths and URLs, and
    report them as warnings.

## 2. Critical: nothing blocks a staged binary (Hooks / ruling 1)

Ruling 1 leaves `.gitignore` to the user.

- **Failure:** a new teammate clones, builds for `local`, and runs
  `git add . && git commit`. The design's pre-commit explodes only "binary edited"
  projects and leak-checks Source, so nothing refuses the staged `*.aprx`. The binary
  is committed and pushed with real local connection strings. The current
  `plan_precommit` blocks this; the redesign drops that block.
- **Direction:** pre-commit refuses any staged path that matches a registered codec
  suffix, and `install` offers to write the ignore rule.

## 3. High: a project open in Pro defeats the sync table (Sync state, open question 4)

- **Failure:**
  1. Pro has `map.aprx` open on branch A.
  2. `git switch B`. post-checkout sees "Source changed", builds B's binary and records
     the hashes.
  3. The user saves in Pro, which writes its in-memory branch A content over the file.
  4. The hash no longer matches and Source is unchanged, so the state reads as
     "binary edited".
  5. The next commit explodes A's content onto B's Source and stages it.

  B's changes are silently reverted, and the table reports a clean, unambiguous state
  throughout. A Pro lock check on `build` doesn't help, because the damage happens on
  the later `explode`.
- **Direction:**
  - Refuse to build while the project is open. Pro may write a lock file such as
    `<name>.aprx.lock`; confirm this on Windows.
  - On "binary edited", diff the exploded content against the recorded Source, not just
    the hashes. If the change touches entries git changed since the record, treat it as
    a conflict.

## 4. High: many git operations change Source without a build hook (Hooks)

- **Failure:** these operations leave Source changed and the binary stale, with no hook
  to rebuild it:
  - `git reset --hard`
  - `git stash pop`
  - `git cherry-pick` and `git revert`
  - `git rebase` (the merge backend doesn't fire post-checkout per step)
  - `git checkout -- <path>` and `git restore`

  The user then edits in Pro, and the state reads "conflict". The user must pick a
  side, and either side loses work. Goal 4 says stop and ask, but the design offers no
  merge, so "ask" always means "discard one". The current code at least had
  post-stash.
- **Direction:**
  - Add post-rewrite and post-stash hooks.
  - Record a git tree ID of the Source at sync time, not an opaque hash. A conflict can
    then be resolved three-way with `git merge-file`: base is the recorded Source, ours
    is the exploded binary, theirs is the current Source.

## 5. High: entry names that differ only in case break the round trip (Codec and Source)

The fixture zip contains both `Map/ec13….json` and `map/map.json`.

- **Failure:** on a case-insensitive filesystem (Windows, macOS), explode merges the two
  directories into one. `pack` then reads names back from disk and emits
  `Map/map.json`, while `GISProject.json` refers to `CIMPATH=map/map.json`. A git
  checkout of such a tree on Windows also shows the files as modified forever. The
  design promises "the same Source gives the same bytes on every machine", but only
  tests on the corpus.
- **Direction:** detect case collisions at explode, and escape names through a manifest
  or record the original name in a sidecar. Add a case-insensitive round-trip test.

## 6. High: reversing by the recorded target is fragile (Explode neutralises, build resolves)

- **Failure (a):**
  1. The user runs `build --target uat` for a deploy. It overwrites the working binary
     and sets `target = uat`.
  2. The Pro session still holds the `local` build, and the user saves.
  3. Explode uses uat's reverse map, so every local value is "unregistered" and the
     commit is blocked.

  If local and uat share a value for one key, it maps to the wrong key silently.
- **Failure (b):** Pro may rewrite connection strings on save: it can reorder the
  `KEY=VALUE` parts, resolve a relative `DATABASE=` to an absolute path after a move,
  or add `AUTHENTICATION_MODE=`. After the first real Pro save, exact string matching
  fails.
- **Direction:**
  - Reverse against the union of every target's values, as the current
    `build_reverse_map` does.
  - Make `build --target` write to a separate output (`-o`) for any non-default target.
  - Compare connection strings as parsed key/value sets, not as raw strings.

## 7. Medium: `check` needs every target's secrets in CI (Value sources, open question 1)

- **Failure:** if open question 1 resolves to "absolute" (no committed value files),
  then to prove Source "resolves for each target", `check` on a pull request needs prd
  credentials from the secret store. On `pull_request` runs, secrets are either
  unavailable (forks) or an exposure risk.
- **Direction:** `check` verifies key coverage against a committed key list per target
  (keys only, no values), and leaves value resolution to the deploy job.

## 8. Medium: the fail-open hook contradicts the leak block (Shape / Hooks)

The design says "only the hook entry point turns [errors] into fail-open warnings". It
also says pre-commit should "block when staged Source fails the leak check". Both
cannot hold.

- **Failure:** on Windows GUI clients (Pro's own git panel, VS Code), the shim's Python
  lookup often fails. Fail-open then lets raw Source commit, with a warning nobody
  reads.
- **Direction:** pre-commit and pre-push fail closed; only the post-* hooks fail open.

## 9. Medium: building during an unresolved merge (Sync state table)

The table's "missing binary → build" and "Source changed → build" rows don't check
`git ls-files -u` for unmerged paths.

- **Failure:** a manual `build`, or a post-checkout on a checkout that leaves conflicts,
  packs JSON containing `<<<<<<<` markers. The passthrough policy writes the
  unparseable entry as raw bytes, so Pro gets a corrupt project and no error.
- **Direction:** `sync.py` refuses to act while the index has unmerged paths under the
  Source dir.

## 10. Medium: "absorbing a Pro re-save" is optimistic (Sync state)

- **Failure:** Pro may change `sourceModifiedTime`, `GpMessages/*`, the view camera and
  `Index.json` on every save. If it does, the in-memory explode will rarely equal the
  recorded Source:
  - every save counts as a real edit
  - every commit carries noise diffs
  - promotion merges conflict on these fields
- **Direction:** a configurable list of volatile fields that explode drops.

## 11. Medium: project discovery and config edge cases (Config / Commands)

- **Project discovery:** hooks and `status` take an optional `[project]`, but the design
  never says how projects are found without one. It could glob `*.aprx.src`, or read a
  list from config.
- **Config search:** "closest file wins" is ambiguous when a nearer `pyproject.toml`
  has no `[tool.cimkit.git]` table.
- **State location in worktrees:** `<git-dir>` is `.git/worktrees/<name>`. Using
  `--git-common-dir` instead would share state between worktrees that have different
  checkouts. Pick one explicitly.
- **Environment variable names:** `CIMKIT_<TARGET>_<KEY>` is ambiguous when a target or
  key contains `_`.

## 12. Low: scope beyond goals

- **Too much before the core works:** several pieces add surface before the sync core
  is proven:
  - Git 2.54 config-based hooks (check the feature exists in released Git before
    depending on it)
  - a codec registry for four formats
  - four value layers
- **`.atbx` entries:** `toolbox.content` is JSON without a `.json` suffix. Rendering by
  suffix leaves such entries opaque and unscanned.
