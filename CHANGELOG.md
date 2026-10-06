# Changelog

All notable changes to this project are documented here. The format is based on
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project adheres to
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Removed

- The deprecated `aprx` command; use `git cim`.
- `git cim install` no longer recognises hooks written by `aprx-tools`. That package
  had no users and has been deleted from PyPI.

## [0.3.0] - 2026-10-06

### Changed (breaking)

- **Renamed `aprx-tools` to `cimkit-git`**, the version-control package of the cimkit
  suite. The PyPI name, import name (`cimkit_git`) and command (`git cim`, installed as
  `git-cim`) all change. Repository files (`aprx.json`, `connections/`, `*.aprx.src/`)
  do not. Hooks installed by aprx-tools call `python3 -m aprx_tools` and fail until you
  re-run `git cim install`, which recognises and replaces them. See
  [Migrating from aprx-tools](README.md#migrating-from-aprx-tools).

### Deprecated

- The `aprx` command. It still works but warns; use `git cim`.

### Added

- `commit_binary` in `aprx.json`. Set it to `true` and simple-mode `git cim verify`
  requires the committed `.aprx` to be present **and** in sync with its source.
  Absent or `false` keeps the default below. A non-boolean value is a configuration
  error.

### Changed

- Simple-mode `git cim verify` no longer fails when the `.aprx` binary is missing.
  The source is the source of truth, so a source-only repository passes. A binary
  that *is* present must still match its source. Use `commit_binary: true` to require
  the binary.
- The strict undeclared-Project guard (ADR-0001) now fires on the **Source-only**
  pre-commit path too, not just on a staged `.aprx` binary. Resolving a merge by editing
  files inside an env Project's `.aprx.src/` and staging the Source only (never the
  binary) used to slip past the guard and pack a derived binary — full of unsubstituted
  tokens, or re-materialised raw connection strings — for a Project that should have a
  blocked or no-committed-binary outcome. Such a commit is now blocked with the same
  `git cim install` diagnostic the binary path already emits. This tightens behaviour toward
  ADR-0001; it does not change simple-mode Source-only commits (merge-conflict
  resolution still packs and stages).
- A commit that stages several undeclared Projects now names all of them in one
  message, instead of one per retry.

### Fixed

- In an environment-mode Project, a merge resolved by hand-editing the tokenised
  `.aprx.src/` and staging only the source is no longer overwritten by the pre-commit
  hook. The hook used to re-explode the working `.aprx`, which can be stale after a
  conflicted merge, and silently discard the resolution. Staged source is now
  re-tokenised in place instead.
- The pre-commit hook no longer aborts the commit when a simple-mode `.aprx` is
  git-ignored. It still repacks the binary so you can open it, but skips staging it.
- An unreadable, non-UTF-8 or malformed `aprx.json` or `connections/*.json` no longer
  crashes the hooks with a traceback. The affected Project is skipped (or the commit
  blocked, where skipping could leak a connection string), with a message saying
  which of the three problems to fix.
- `git cim install` and `git cim connections init` stop with a clear message on a
  broken `aprx.json`. Previously `install` could treat an unparseable file as missing
  and overwrite it, losing its `fields` and `token` settings.

## [0.2.1] - 2026-06-25

### Fixed

- `aprx build <dir>` pointed at a directory **not** named `*.aprx.src` no longer
  produces an output path that collides with the input directory (the old derivation
  reduced `my-folder` to `my-folder`); it now writes `my-folder.aprx`.

### Changed

- `util` is now the single owner of the `.aprx` ↔ `.aprx.src` naming convention.
  Duplicated derivations in `hooks` (`_aprx_for`, an inline `src_dir` build) and the
  caught-`ValueError` fallback in `pack` are gone; every caller routes through
  `util.aprx_for_src_dir` / `src_dir_for` / the new lenient `aprx_output_for`. Pure
  internal refactor — no change to committed output.
- The last open-coded git-root finder (`hooks._git_root`) now routes through
  `util.git_root`, leaving one git-root implementation in the package.

### Added

- `util.aprx_output_for`: best-effort src-dir → `.aprx` naming for commands a user can
  aim at any directory (`pack`, `build`) — strips a trailing `.src`, ensures `.aprx`,
  and never raises (unlike the strict `aprx_for_src_dir`).

## [0.2.0] - 2026-06-25

### Changed (breaking)

- **A project's mode is now declared, not detected.** The mode (`simple` or `env`) is
  read from a `"mode"` field in a committed `aprx.json` and is the single source of
  truth. The previous heuristic — inferring environment mode from the mere presence of
  an `aprx.json`, a `connections/` directory, or a `local.json` — has been removed.
  Resolution is strict: a project with no `aprx.json`, or one whose `aprx.json` has no
  `mode`, is a hard error that directs you to run `aprx install`. There is no
  back-compat inference.

  **Migration (one-time, per project):** run `aprx install` from the project's
  directory to record the mode (or `aprx install --mode simple|env` to set it without
  a prompt), then commit the resulting `aprx.json`. See
  [Upgrading an existing repository](README.md#upgrading-an-existing-repository).

### Added

- `aprx install` records the project mode in `aprx.json`: it prompts on first run,
  accepts `--mode simple|env` to bypass the prompt, defaults to `simple` (with a loud
  warning) in a non-interactive shell with no existing config, and honours an existing
  declaration without prompting — refusing a conflicting `--mode` rather than silently
  overwriting a shared team decision.
- `aprx connections init` records `"mode": "env"` in `aprx.json` as it scaffolds the
  connection files.
