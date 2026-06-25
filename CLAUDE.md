# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

CLI + git-hook tooling that makes ArcGIS `.aprx` project files version-controllable.
An `.aprx` is a zip of JSON (Pro 3.x) / XML (Pro 2.x) entries. The tool *explodes*
it into a diffable `<name>.aprx.src/` directory, *packs* that directory back into an
`.aprx`, and installs git hooks so the conversion is automatic. Both the binary and
the exploded source are committed; the `.aprx.src/` is the canonical source of truth
and the `.aprx` is a regenerated artifact.

## Commands

```sh
make dev-setup     # uv sync --extra test + install dev pre-push hook
make test          # uv run pytest (full suite)
uv run pytest tests/test_pack.py            # single file
uv run pytest tests/test_pack.py::test_name # single test
uv run pytest -k roundtrip                  # by keyword

# Run the CLI from source
uv run aprx explode map.aprx          # → map.aprx.src/
uv run aprx pack    map.aprx.src/     # → map.aprx
uv run aprx compare a.aprx b.aprx     # semantic diff; exit 1 if differs
uv run aprx install                   # install git hooks into the cwd's repo
```

`uv` is required only for developing aprx-tools itself. There is no linter configured.

## Architecture

Single Python package `aprx_tools/`, dispatched through `__main__.py` (argparse).
Each command lazy-imports its module. The data flow is a round-trip:

```
explode.py:  .aprx (zip)  ── format JSON/XML pretty ──▶  .aprx.src/   (committed, diffable)
pack.py:     .aprx.src/   ── minify JSON ──▶  .aprx (zip)            (derived artifact)
compare.py:  normalises both sides and unified-diffs them (works on files OR dirs)
```

- **`util.py`** owns the naming convention that everything else depends on:
  `map.aprx ↔ map.aprx.src` (note: a `.src` dir is only recognised when it ends in
  `.aprx.src` *and* contains `GISProject.json`). Changing this convention ripples
  through hooks, pack, and explode.

- **`hooks.py`** is the logic the installed git hooks call via `python3 -m aprx_tools hook <name>`.
  `hook_pre_commit` has two layers worth understanding before editing:
  - **Env-managed projects first**: for each existing `.aprx.src/` that sits in a
    connection-substitution project, stage neutral (tokenised) source only — the binary is
    never committed. *How* the source is neutralised depends on whether the developer staged
    Source inside that project this commit (issue 0007): with **no staged Source** it is
    re-exploded from the (gitignored) working `.aprx` (`_refresh_env_source` — the normal
    flow, where the developer edited the binary); with **staged Source** (a hand-resolved
    merge) it is instead re-tokenised *in place* (`_retokenize_staged_source`), preserving the
    developer's content rather than re-deriving it from a possibly-stale binary that would
    silently clobber the resolution. The staged-Source signal counts deletions too, so a merge
    that drops an entry is not resurrected from the binary.
  - **Simple projects** (the original two-pass flow): (1) any staged `.aprx` is exploded,
    its `.src/` staged, and the binary unstaged; (2) any staged file inside a `.aprx.src/`
    triggers a repack + re-stage of the `.aprx`. Pass 2 runs independently so it also
    handles merge-conflict resolution (developer edited `.src/` JSON directly). Env-managed
    src dirs are skipped here so their binary is not staged.
  `hook_post_merge` / `hook_post_checkout` / `hook_post_stash` all call `build_working_copies`,
  which repacks each `.aprx.src/` into its local working `.aprx` (default `local.json`),
  skipping env projects that have no resolvable connections file.
  `hook_pre_push` runs `aprx verify` — the local mirror of the CI gate — and returns its exit
  code to block a push of an untokenised or unbuildable source (`git push --no-verify` bypasses).
  Five hooks are installed in total (`pre-commit`, `pre-push`, `post-stash`, `post-merge`,
  `post-checkout`); the `pre-push` script omits the install-hint wrapper so `verify`'s own
  diagnostics show.

- **`install.py`** writes the hook scripts into `.git/hooks/`. Hooks are tagged with the
  `managed-by: aprx-tools` marker — install overwrites its own hooks but refuses to clobber
  foreign ones (prints manual-integration instructions instead). The hook scripts probe for
  a repo-local `.venv`/`venv`/`env` Python before falling back to `python3`.

### Connection-string substitution (environment mode)

A project's **Mode is declared, not detected** (ADR-0001). It is read from a `"mode"`
field (`"simple" | "env"`) in a committed `aprx.json` via `ProjectConfig.load`
(`project_config.py`) — the one object that owns the resolution ritual (mode, fields,
token, connection-file discovery). There is **no presence-sniffing**: the tool never
infers a mode from a stray `connections/` dir or `local.json`. Resolution is **strict**
— a project with no `aprx.json`, or one whose `aprx.json` omits `mode`, is a hard exit
directing the user to `aprx install`. Two modes coexist and the code must preserve both:

- **Simple mode** (`"mode": "simple"`): explode/pack behave exactly as before — this is
  what keeps the existing tests green. The CLI hands the core the no-op `IDENTITY`
  transform.
- **Environment mode** (`"mode": "env"`): `explode` replaces configured field values
  (default `workspaceConnectionString`) with `@@token@@` placeholders; `pack` substitutes
  them back for a chosen environment. The CLI builds a `Substitution` transform from the
  `ProjectConfig` and injects it (see ADR-0002, the transform seam).

`aprx install` is the opt-in point that writes the `mode` into `aprx.json` (prompt,
`--mode simple|env`, or non-TTY simple default); `connections init` writes `"mode": "env"`
as it scaffolds. An existing declaration is honoured untouched.

- **`connections.py`** is the pure engine. It operates on **parsed JSON objects, never text**,
  so connection strings containing `\`, `;`, `"` re-serialise with correct escaping.
  `tokenize` (value → token, on explode) and `substitute` (token → value, on pack) return the
  set of *unknown values* / *missing keys* so callers fail fast — an unregistered connection
  string aborts explode; a missing env key aborts pack, before any output is written.
  `resolve_connections_file` (precedence: `--connections` > `--env` > `local.json`) and
  `build_reverse_map` (union across all env files; errors if one value maps to two keys) live
  here too — driven by `ProjectConfig`, not by file-presence detection.
- **`bootstrap.py`** implements `aprx connections init` (scan an `.aprx` for distinct connection
  strings, scaffold `aprx.json` + `connections/dev.json` + `local.json.example`) and
  `aprx connections check` (assert every `connections/*.json` defines the same key set).
- **`verify.py`** implements `aprx verify` — the single exit-coded CI gate (CI-agnostic by
  design; any runner calls it). Env mode: source is fully tokenised (`scan_tokens` finds no raw
  values) and every referenced key resolves in every env file. Simple mode: committed `.aprx`
  matches `pack(src)`. README "Continuous integration" has per-provider trigger snippets.
- The working `.aprx` is a gitignored build artifact in env mode; `aprx build` (and the
  post-merge/post-checkout hooks) regenerate it from source + `local.json`.

### Determinism (do not break this)

`pack.py` is deliberately reproducible so the committed binary is stable across machines/runs:
- every zip entry is written with a fixed DOS-epoch timestamp `(1980,1,1,0,0,0)`
- files are enumerated in sorted order
- `compresslevel=6` is pinned
Removing any of these reintroduces non-deterministic binaries. The README's "Testing"
section notes byte-for-byte determinism is not yet explicitly tested — keep it in mind when
touching pack.

### Format handling

- explode pretty-prints `.json` (indent 2, `ensure_ascii=False`) and indents `.xml`;
  pack minifies `.json` (`separators=(",",":")`). XML is currently passed through on pack
  (not re-minified).
- Anything that is not parseable JSON/XML — thumbnails, `.dat` blobs — must be copied as raw
  bytes. Both explode and pack fall back to byte passthrough and warn on parse failure rather
  than aborting. Preserve this fallback; corrupting binary entries is the main silent-failure risk.

## Distribution

This repo publishes a single Python package to **PyPI** (`pyproject.toml`): the actual
package + the `aprx` entry point. Releases are automated by `.github/workflows/release.yml`
(publish a GitHub Release → trusted-publishing upload to PyPI; see README "Releasing").

`aprx_tools/install.py` generates the five git hooks (`pre-commit`, `pre-push`,
`post-stash`, `post-merge`, `post-checkout`) from a small factory (`pre-commit` /
`pre-push` block on failure; the `post-*` hooks never block).
`__version__` lives in `aprx_tools/__init__.py` and is mirrored in `pyproject.toml` —
bump both together.

## Tests

`pytest`, fixtures in `tests/fixtures/`. `conftest.py` exposes a single `simple.aprx`
(Pro 3.x / JSON) fixture and an `exploded` fixture. The suite covers the explode/pack/compare/
install round-trips against that one fixture. Known coverage gaps (Pro 2.x XML format, binary
passthrough, determinism, non-ASCII, multi-map projects, cross-platform) are documented in the
README "Testing" section — consult it before claiming an area is tested.
