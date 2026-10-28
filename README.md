# cimkit-git

Version control for ArcGIS Pro project files. Part of the
[cimkit](https://github.com/rileylum/cimkit) suite of headless ArcGIS tools.

An `.aprx` is a zip of JSON and XML. Git can't diff or merge it, and it holds
machine-specific connection strings. cimkit-git explodes each project into a diffable
directory, `map.aprx.src/`, which is what you commit. It builds the `.aprx` back from that
directory when you need it, and its git hooks stop a commit that would lose a Pro edit or
leak a connection string.

- **Source is the only committed form.** The `.aprx` is a git-ignored working copy.
- **Environment values never reach the repo.** Source holds placeholders such as
  `@@main_gdb@@`; the values live in a git-ignored file or in environment variables, and
  a build fills them in for the target you name.
- **Hooks check; they never write.** You run `sync`, `explode` or `build` yourself.
- **It never guesses.** When both the `.aprx` and its Source changed, it stops and asks.

`.aprx` from Pro 3.x is supported today. Pro 2.x projects are refused: open and save them
in Pro 3 first. `.atbx`, `.lyrx` and `.stylx` are planned.

## Install

Requires Python 3.11 or later, and no `arcpy`.

```sh
pip install cimkit-git
```

Then, in your repository:

```sh
cimkit-git install
```

`install` makes git run the checks on commit, push and checkout. It also adds `*.aprx` and
`cimkit.local.toml` to `.gitignore`, and a line to `.gitattributes` that stops git
converting line endings in Source. It records the Python it runs under, so run it from the
environment cimkit-git is installed in, and again if that changes. `cimkit-git uninstall`
removes the hooks and leaves the ignore lines.

## Quick start

```sh
cimkit-git status         # what each project needs
cimkit-git sync           # explode or build, whichever status calls for
git add map.aprx.src
git commit -m "Add the map"
```

After saving in Pro, run `sync` to explode the edit into Source, then commit. After a pull
or checkout, run `sync` to build the `.aprx` from the new Source. The hooks remind you
when a project needs it.

| Command | Does |
|---|---|
| `status` | prints each project's status |
| `sync [project]` | explodes or builds, whichever the status calls for; refuses when both sides changed |
| `explode [project] [--force]` | writes Source from the `.aprx` |
| `build [project] [--target T] [--discard] [-o FILE]` | writes the `.aprx` from Source; with `-o`, writes a deploy copy and leaves the working `.aprx` alone |
| `check [--rev REV] [--target T]` | the CI gate: committed Source holds no real values, every entry parses, and with `--target` that target has every value |
| `install`, `uninstall` | register or remove the git hooks |

With no `project` argument, a command acts on every `*.aprx` and `*.aprx.src` under the
config file's directory. `python -m cimkit_git` runs the same command.

## Environments

With no config, nothing is replaced: Source holds the connection strings as Pro wrote
them, and `check` errors on none of them. To keep them out of the repo, declare
placeholders in `cimkit.toml`, or in `[tool.cimkit.git]` in `pyproject.toml`:

```toml
[git]
exclude = ["archive/**"]          # optional: globs of projects to skip

[git.placeholders]
fields  = ["workspaceConnectionString"]
format  = "@@{key}@@"
keys    = ["main_gdb"]
targets = ["local", "dev", "prd"]  # the first is the default
```

The committed config holds names only. Each target's values come from
`cimkit.local.toml`, which `install` git-ignores:

```toml
default_target = "dev"            # optional: overrides the first target on this machine

[local]
main_gdb = 'DATABASE=C:\data\main.gdb'

[dev]
main_gdb = "SERVER=dev-db;INSTANCE=sde:postgresql:dev-db;DATABASE=main"
```

An environment variable `CIMKIT__<TARGET>__<KEY>`, such as `CIMKIT__PRD__MAIN_GDB`, wins
over the file. CI supplies values that way.

When `explode` or `sync` finds a connection string that maps to no key, it offers in a
terminal to register it: it suggests a key from the `.gdb` or database name, adds the key
to `cimkit.toml` and the value to `cimkit.local.toml`. Without a terminal, or with
`--no-input`, it prints the steps and stops.

## Continuous integration

`check` is the gate, and any runner can call it. Without `--target` it needs no secrets,
so it is safe on pull requests from forks. A deploy job adds `--target` and that target's
values:

```yaml
# .github/workflows/check.yml
on: [push, pull_request]
jobs:
  check:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v4
      - uses: actions/setup-python@v5
        with:
          python-version: "3.12"
      - run: pip install cimkit-git
      - run: cimkit-git check
```

```yaml
      # In the deploy job:
      - run: cimkit-git check --target prd
        env:
          CIMKIT__PRD__MAIN_GDB: ${{ secrets.PRD_MAIN_GDB }}
      # A fresh clone has no .aprx, so name the Source.
      - run: cimkit-git build map.aprx.src --target prd -o dist/map.aprx
        env:
          CIMKIT__PRD__MAIN_GDB: ${{ secrets.PRD_MAIN_GDB }}
```

## Hooks

| Hook | Does |
|---|---|
| pre-commit | blocks a staged `.aprx`, a project whose `.aprx` holds an edit Source lacks, and staged Source that leaks a value |
| pre-push | blocks a push if any outgoing commit fails `check` |
| post-checkout, post-merge, post-rewrite, post-stash | says which projects need a `sync` |

`git commit --no-verify` and `git push --no-verify` skip them; `check` in CI catches the
rest.

On Git 2.54 or later, `install` registers the hooks in the repo's git config, beside any
hooks you already have. On older Git it writes hook scripts, and leaves any hook it
didn't write alone, printing the line to add to it.

### With the pre-commit framework

If your team uses [pre-commit](https://pre-commit.com), list the hooks in
`.pre-commit-config.yaml` instead:

```yaml
repos:
  - repo: https://github.com/rileylum/cimkit-git
    rev: v0.4.0
    hooks:
      - id: cimkit-git-pre-commit
      - id: cimkit-git-pre-push
      - id: cimkit-git-post-checkout
      - id: cimkit-git-post-merge
      - id: cimkit-git-post-rewrite
default_install_hook_types: [pre-commit, pre-push, post-checkout, post-merge, post-rewrite]
```

Run `cimkit-git install` as well: in a repo that uses the framework it adds the ignore
lines and registers no hooks. The framework has no post-stash stage, and its pre-push
stage passes only the first branch of a push, so a push of several branches checks only
the first.

## Limits

- Pro 3.x `.aprx` only.
- No `compare` or semantic diff yet; `git diff` on Source covers most of it.
- A conflict, where both the `.aprx` and its Source changed, is refused, not merged.
- Not yet tested on Windows or against a binary that Pro opens after a build.

## Contributing

This project uses [uv](https://github.com/astral-sh/uv):

```sh
make dev-setup   # creates the venv, installs the test extra and a pre-push hook
make test        # runs the suite
```

`docs/design.md` is the full design.

## Releasing

Publishing a GitHub Release runs `.github/workflows/release.yml`, which builds the package
and uploads it to PyPI by trusted publishing.

1. Bump the version in `pyproject.toml` and `cimkit_git/__init__.py`, and date the
   CHANGELOG entry.
2. Commit, then publish a GitHub Release tagged with the version, such as `v0.4.0`.

PyPI rejects a version it already has, so every release needs a new number.
