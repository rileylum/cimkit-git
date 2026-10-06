# cimkit-git redesign

Status: draft 3, revised after the adversarial reviews in
`plans/2026-10-06-design-challenge-codex.md` and `plans/2026-10-06-design-challenge-fable.md`.
Nothing here is built yet.

## Goals

- Version control for Esri project files. `.aprx` is the MVP; `.atbx`, `.lyrx` and
  `.stylx` follow.
- Environment information never reaches the repo. Projects promote through
  dev → uat → prd branches, and building any commit for any target also works.
- Usable standalone, and composable with the other cimkit packages.
- Never lose an edit. When the tool can't tell which side changed, it stops and asks.

## Rulings

| # | Question | Ruling |
|---|---|---|
| 1 | Commit the binary? | No. Source is the only committed form. `install` writes the ignore line, and pre-commit refuses a staged binary. |
| 2 | Promotion model | Branches, with build-per-target also supported. Environment values never reach the repo. |
| 3 | Who writes? | Explicit commands only. Hooks check and refuse; they never write. |
| 4 | Config format | TOML. Python 3.11 is the floor (`tomllib`). |
| 5 | Where do values live? | Committed config declares targets and keys. Values live only in a git-ignored local file or environment variables. |
| 6 | Machine paths outside configured fields | `check` warns. Whether to strip them waits for corpus testing. |
| 7 | Old ADRs, PRD, issues | Deleted. Nothing from them is binding. |

## Vocabulary

- **Project**: one Esri project file, such as `map.aprx`.
- **Binary**: the file the GUI opens and saves. A git-ignored working copy.
- **Source**: the committed, diffable rendering of a project: `map.aprx.src/`.
- **Codec**: reads a binary into entries and writes entries back. One per format.
- **Sync state**: what the tool last wrote or read on each side, kept in the git dir.
- **Placeholder**: the neutral stand-in for an environment value in Source, such as
  `@@main_gdb@@`.
- **Key**: the name inside a placeholder, such as `main_gdb`.
- **Target**: a named environment to build for, such as `local`, `dev` or `prd`.

There is no mode. A project with no placeholder config has nothing to replace.

## Shape

```
formats/zip_json.py  codec for .aprx and .atbx
source.py            entries <-> Source dir: name validation, rendering, passthrough
placeholders.py      pure: neutralise / resolve / scan; returns problems
values.py            a target's values from the local file and environment variables
config.py            finds and parses cimkit.toml / pyproject.toml
state.py             sync state: read, compare, record
sync.py              decides what a project needs, from files, index and state
git.py               thin git wrapper
cli.py               the only module that prints, prompts or exits
```

Library code raises typed exceptions. `cli.py` maps them to exit codes. How a hook
treats a failure is set per hook (see Hooks), never by a blanket rule.

Further formats get a codec when they're built. There's no registry until a second
codec exists.

## Codec and Source

- A codec exposes `read(path) -> entries` and `write(entries, path)`. Entries are
  `(name, bytes)` pairs in a stable, sorted order.
- **Name validation runs before any file is written.** Explode rejects:
  - absolute paths, `..` segments, and drive or UNC forms
  - duplicate names
  - symlinks in Source
- **Case collisions are escaped, not merged.** The fixture already has both `Map/` and
  `map/`. When two names differ only in case, the one that sorts first keeps its name
  and each later one gets a `~N` suffix on the colliding segment (`Map/` → `Map~2/`).
  `_names.json` at the Source root maps escaped names back to the originals; it exists
  only when a project has a collision. Adding or removing a colliding sibling can shift
  the assignment and cause one noisy diff; that is accepted as rare.
- **Entry kind is decided by content, not suffix.** `.atbx` holds JSON in files without
  a `.json` suffix.
- **Rendering:** JSON is pretty-printed and XML indented. Known opaque entries
  (thumbnails, blobs) are copied as raw bytes.
- **A JSON or XML entry that fails to parse is an error, not passthrough.** Unparseable
  structured data can hide an environment value from the leak scan. `explode --raw`
  keeps the bytes for recovery but never counts as a clean scan.
- **Pack is deterministic:** sorted entries, fixed zip timestamps, pinned compression
  level. The same Source gives the same bytes on every machine.

## Sync state

The state lives in `$(git rev-parse --git-dir)/cimkit/state.json`. That is per worktree,
because each worktree has its own checkout. It holds one record per project:

- `source_tree`: the git tree ID of the Source at the last sync.
- `binary_hash`: hash of the binary the tool last wrote or read.
- `target`: the target the binary was built for.
- `mapping`: for each value the build used, a hash of the value and the key it came
  from. Explode reverses through this, so a value that changes later can't map back to
  the wrong key. The state stores hashes only, so no secret is written to disk in plain
  text.

### Status

`sync.py` derives a status from the binary, the working Source, the index and the
record:

| Binary | Source | Status | `sync` does |
|---|---|---|---|
| same | same | clean | nothing |
| changed | same | binary edited | explode |
| same | changed | Source changed | build for the recorded target |
| changed | changed | conflict | refuses |
| missing | any | not built | build for the default target |
| present | missing | new project | explode |
| present | present | no record | refuses; the user runs `explode` or `build --discard` |

Before using the table, `sync.py` checks three things and refuses if any holds:

- files under the Source dir are unmerged (`git ls-files -u`)
- the index holds Source that differs from both the working Source and the recorded
  tree, so a staged edit would be lost
- the tool lock is held, or Pro's lock file for the project exists (open question 1)

A binary whose hash changed but whose in-memory explode equals the recorded Source
counts as unchanged.

Modification times are never used, because git rewrites them on checkout.

Recording the Source as a git tree means a conflict can later be resolved three-way:
the base is the recorded tree, one side is the exploded binary, the other is the current
Source. That merge is deferred. For now, conflict means refuse and tell the user.

### Write protocol

Every write follows the same steps:

1. Take the tool lock (`<git-dir>/cimkit/lock`).
2. Re-check the status.
3. Write the complete output to a temp path beside the destination.
4. Check the binary or Source being replaced still matches the record.
5. Rename the temp output into place.
6. Record the new state.

A failure at any step leaves the previous output and the previous record untouched.

## Commands

| Command | Does | Refuses when |
|---|---|---|
| `sync [project]` | the one unambiguous action from the status table | conflict, no record, or any pre-check fails |
| `explode [project]` | binary → Source | Source changed since the record (unless `--force`) |
| `build [project] [--target T] [-o FILE]` | Source → binary | binary edited since the record (unless `--discard`) |
| `status` | prints each project's status | never |
| `check [--rev REV] [--target T]` | the CI gate (see Checks) | — |
| `install` | registers hooks, writes the ignore line | — |
| `hook <name>` | hidden; called by the hook shims | — |

`build --target T -o FILE` writes a deploy artifact and leaves the working binary and
its record alone. Without `-o`, `build` changes the working binary and its record.

With no `project` argument, a command acts on every project. A project is any `*.aprx`
or `*.aprx.src` under the config file's directory, minus the globs in an optional
`exclude` list. Projects are never listed in config, so a new map needs no config edit.

The tool is the single command `cimkit-git`. There is no `git cim` alias: git turns
`git cim --help` into a man-page lookup that fails on pip installs and on Windows. The
`cimkit` umbrella package can later dispatch `cimkit git …`.

`compare` and `diff` are deferred.

## Hooks

Each hook is a one-line shim that calls `cimkit-git hook <name>`. The decisions live in
`sync.py`, so a hook always agrees with `status`.

| Hook | Checks | On a finding | If it can't run |
|---|---|---|---|
| pre-commit | any staged binary; any project not clean; staged Source (read from the index) failing the leak check | blocks | blocks |
| pre-push | every outgoing commit, from the refs git passes on stdin | blocks | blocks |
| post-checkout, post-merge, post-rewrite, post-stash | prints projects that are not clean, and the command to run | warns | warns |

pre-commit blocks on any project that isn't clean. A binary edit that isn't in Source
yet stops the commit with "run `cimkit-git sync`", so a commit never silently misses a
Pro edit.

`install`:

- On Git 2.54 or later, registers the hooks in git config (`hook.<name>.*`). Git 2.55
  supports `git hook list`; this was confirmed locally.
- On older Git, writes shims into `git rev-parse --git-path hooks`, and never
  overwrites a hook it didn't write.
- Records the absolute path of the Python it runs under, because Git's `sh` on Windows
  won't find the Pro conda environment on its own.
- Adds the binary ignore line (for example `*.aprx`) and `cimkit.local.toml` to
  `.gitignore`.

The repo also ships `.pre-commit-hooks.yaml` for teams that use the pre-commit
framework. cimkit-git needs no `arcpy`, so the framework's isolated virtualenv works.

## Environments

### Config

```toml
[git.placeholders]
fields  = ["workspaceConnectionString"]
format  = "@@{key}@@"
keys    = ["main_gdb", "archive_gdb"]
targets = ["local", "dev", "uat", "prd"]
```

- Every target must define every key. The committed config holds names only, never
  values.
- Key and target names are lowercase `[a-z0-9_]`.
- The first entry in `targets` is the default target. A developer can override it for
  their own machine with `default_target` in `cimkit.local.toml`.
- With no `[git.placeholders]` table, nothing is replaced.

### Values

Values come from two layers. The later layer wins:

1. `cimkit.local.toml`, git-ignored, with one table per target:
   `[local] main_gdb = "..."`.
2. Environment variables: `CIMKIT__<TARGET>__<KEY>`, upper-cased, with `__` as the
   separator.

CI supplies layer 2 from its secret store.

### Explode neutralises, build resolves

- **Build for target `T`** replaces each placeholder with `T`'s value. A missing value
  is an error, so a binary is never written with placeholders left in it.
- **Explode** replaces values through the recorded mapping. With no record, it uses
  every value it can see locally.
- **Unregistered values** are an error: a value in a configured field that maps to no
  key. Placeholders never mix with real values.
- **Registering a new value.** In an interactive terminal, `sync` and `explode` offer
  to register each unregistered value. They suggest a key from the `.gdb` or database
  name and ask before writing. The key goes into `keys` in `cimkit.toml` (committed)
  and the value into `cimkit.local.toml` for the current target. Without a terminal,
  and always in hooks, they print the same steps and stop. `check --target T` then
  fails until every other target has a value for the new key.

Source holds placeholders on every branch, so promotion merges never conflict on
environment values.

## Checks

`check` reads committed content: the working tree by default, or `--rev REV`. pre-push
runs it on each outgoing commit. It reports:

| Finding | Severity |
|---|---|
| a configured field holds a value that isn't a placeholder | error |
| a placeholder names a key that config doesn't declare | error |
| a JSON or XML entry fails to parse | error |
| any string holds an absolute or UNC path, or a URL | warning |
| with `--target T`: `T` lacks a value for a declared key | error |

Without `--target`, `check` needs no secrets, so it is safe on pull requests from forks.
The deploy job runs `check --target prd` with that target's secrets.

## Config discovery

- The tool looks for `cimkit.toml` (table `[git]`), then the `[tool.cimkit.git]` table
  in `pyproject.toml`.
- It walks up from the project and stops at the git root. A `pyproject.toml` without
  the table doesn't count, and the search continues upward. The first match wins; files
  are never merged.
- `--config` overrides the search. Paths in config resolve relative to the config file.
- Each cimkit package reads only its own table, so each works standalone.

## Testing

- Behaviour tests at the command and hook level, in throwaway git repos. These cover
  the index and worktree divergence, unmerged paths, and outgoing commits on pre-push.
- Pure tests for `placeholders`, `state` and the `sync` decisions.
- Format tests: name validation, case collisions, content-based entry kinds.
- Properties the old suite never covered: byte-for-byte determinism, binary passthrough,
  and the round trip on the `cimkit-corpus` projects.

## Removed from the old design

- modes and `aprx.json`
- the two-phase transform object and `StagePlan` routing
- committed binaries and `commit_binary`
- `connections init` as a separate command
- hooks that write
- `sys.exit` inside library code
- passthrough for unparseable JSON

## Open questions

1. **Pro's lock file.** What does Pro write while a project is open, and where? This
   needs testing on Windows.
2. **Volatile fields.** Does Pro change `pathHint`, timestamps or camera state on every
   save? Run the corpus through Pro and compare, then decide whether to strip them.
3. **Connection-string rewriting.** Does Pro reorder or expand connection strings on
   save? If so, values must be compared as parsed key sets, not raw strings.
