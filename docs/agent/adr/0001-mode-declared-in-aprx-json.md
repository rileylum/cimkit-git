# Mode is declared in a committed `aprx.json`, not detected

## Status

accepted

## Context & decision

aprx-tools serves two purposes — plain version control (**simple mode**) and version
control plus connection substitution (**environment mode**). A project's mode is now
declared explicitly in a committed `aprx.json` (`"mode": "simple" | "env"`, alongside
`fields`/`token`) and is the single source of truth. `aprx install` writes the file on
first run; every teammate's hooks then read the same value, so the team can't diverge.

This replaces the previous heuristic, which inferred environment mode from the mere
*presence* of `aprx.json`, a `connections/` directory, or a `local.json`. Mode now also
governs behaviour as the master switch: in simple mode `--env` is an error; in environment
mode `explode` **always** tokenizes (so a direct `aprx explode` can never leak a raw
connection string), and flags only refine *which* environment's values `pack` substitutes.

## Considered options

- **Heuristic detection (status quo)** — rejected: three independent signals could drift,
  two developers could resolve the same repo to different modes, and a misdetected env
  project would silently commit raw connection strings.
- **A separate config file for mode** — rejected: `aprx.json` already exists, is already
  committed and shared, and already carries `fields`/`token`; a second file splits one
  decision across two places.
- **Flag-as-master (`pack` basic unless `--env`)** — rejected for `pack`: a bare `pack`
  in an env project would emit a binary full of unsubstituted tokens (a broken `.aprx`).

## Consequences

- **Strict migration, no inference.** Loading a project with no `aprx.json`, or one without
  a `mode`, is a hard error directing the user to run `aprx install`. Existing repos break
  until re-installed — acceptable at v0.1.0 alpha; needs a CHANGELOG/README upgrade note.
- Simple-mode projects now also commit an `aprx.json` (`"mode": "simple"`) — the explicit
  opt-in record.
- `aprx install` with no TTY and no existing config defaults to simple mode and warns;
  `--mode simple|env` bypasses the prompt everywhere.
- **Decide-and-write applies only to an *absent* `aprx.json`.** A *missing* file is the
  fresh-install case install exists to fix: it decides a mode and writes a new config. A
  file that is *present but unreadable / non-UTF-8 / malformed / not a JSON object* is
  **not** treated as "no mode on record" — install reports a directed error (a hard exit
  naming the read/decode/parse failure) and leaves the file untouched, rather than
  overwriting a committed config it merely failed to *parse* and silently discarding the
  developer's `fields`/`token` (you cannot preserve fields you cannot read). Both the
  resolution path (`ProjectConfig.load`) and the two bootstrap-time reads (`install`,
  `connections init`) route the read/decode/parse through one shared loader so a broken
  `aprx.json` is reported identically everywhere; only the *absent* case differs (the
  bootstrap commands create the file, the resolution path errors directing to `install`).
