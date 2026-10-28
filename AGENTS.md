# cimkit-git

Version control for ArcGIS Pro project files, one package of the cimkit suite. Sibling
repos sit beside this one in `~/dev/cimkit/`. `docs/design.md` is the design and its
rulings; read the section a change touches before making it. `README.md` is the user's
view.

## Vocabulary

Use the design's terms: **project**, **binary** (the git-ignored `.aprx`), **Source**
(the committed `map.aprx.src/`), **codec**, **sync state**, **placeholder**, **key**,
**target**. There is no "mode".

## Commands

```sh
make dev-setup                       # uv sync --extra test, and a pre-push hook that runs the suite
uv run pytest -q -p no:cacheprovider # the suite, about 30 seconds
uv run --no-project --python 3.11 --with pytest python -m pytest -q -p no:cacheprovider
uv run pyright <files>               # check only the files you change
uv run cimkit-git <command>          # the CLI from source
```

On Python 3.11, expect two strict xfails (minidom; see the design's "Codec and Source")
and the pre-commit framework tests skipped, since that run has no `pre-commit`.

## Modules

```
formats/zip_json.py  codec for .aprx: read and write entries
source.py            entries <-> Source files: names, case escapes, rendering
placeholders.py      pure: neutralise, resolve and scan values in configured fields
values.py            a target's values, from cimkit.local.toml and CIMKIT__ variables
config.py            finds and parses cimkit.toml or [tool.cimkit.git]
state.py             sync records and the tool lock, in the git dir
sync.py              Workspace: status decisions, explode, build, sync
checks.py            the leak, parse, name and path findings check reports
register.py          adds a new key and value to the config files, keeping their layout
replace.py           atomic replacement of a binary or a Source dir
installer.py         install and uninstall the git hooks; the ignore lines
git.py               thin wrapper over the git commands the rest need
cli.py               the only module that prints, prompts or exits
```

## Invariants

- **Never lose an edit.** When both the binary and Source changed, refuse. Every write
  follows the write protocol in the design: lock, re-check, temp file, rename, record.
- **Deterministic build.** Sorted entries, fixed zip timestamps, pinned compression.
  Compare binaries by entry hash, never zip bytes.
- **Values stay out of the repo, the state and the messages.** State holds hashes only.
  Exception messages never hold a value; `cli.mask` hides passwords on screen.
- **Hooks check; they never write.**
- **Library code raises typed errors** under `CimkitError` in `errors.py`. Only `cli.py`
  prints, prompts or exits.
- Python 3.11 floor, stdlib only, no `arcpy`.

## Tests

- Test at the command and hook level, in throwaway git repos. Reuse the helpers:
  `tests/test_git.py` (`repo`, `run`, `commit`), `tests/test_cli_status.py`
  (`project`, `configured`, `record_sync`, `write_source`, `ENTRIES`, `NEUTRAL`, `CONFIG`,
  `layer`), `tests/test_cli_check.py` (`committed`, `SECRET`), `tests/test_cli_hooks.py`
  (`hook`, `remote`, `push`, `leak`, `ZERO`) and `tests/test_cli_install.py` (`install`,
  `uninstall`, `git_commit`, `importable`, `hook_config`).
- A test in which real git runs a hook needs the `importable` fixture, or the 3.11
  `--no-project` run can't import `cimkit_git`.
- `tests/test_pre_commit_framework.py` runs `pre-commit try-repo`, which reads only
  tracked files: stage a new `.pre-commit-hooks.yaml` before running it.
- Help text must not contain the word "hook": a test guards the hidden `hook` subcommand.
- `tests/fixtures/simple/simple.aprx` holds a `pathHint` with personal names. Don't copy
  that value into docs or tests.
- Corpus tests run when `../cimkit-corpus/sources` (or `$CIMKIT_CORPUS`) exists, and
  skip otherwise.

## Lint

Ruff isn't configured. F811 on fixture imports is the repo's pattern. I001 on `cli.py`
and PLW1510 on `git.ignored` predate the redesign. Don't run `ruff --fix --select I` on
whole files. Pass `check=False` to `subprocess.run` in tests.

## Git

The repo is public. Branch from `redesign` until it merges, and open each slice as its
own PR into it. Ask before pushing; the user merges.
