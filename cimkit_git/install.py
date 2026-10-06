"""Install git hooks into the current (or specified) repository.

`git cim install` is also the **opt-in point for a Project's Mode** (ADR-0001). On
first run it decides the Mode (`simple` | `env`) and records it in the committed
`aprx.json`; every later run honours that declaration so the whole team's hooks
behave identically. The decision and the config write live here; `ProjectConfig`
(project_config.py) reads the declaration back when a command resolves a Project's
mode. (The git hooks themselves still presence-sniff today; issue 0009 switches
them to read the recorded mode.)"""

import stat
import sys
from pathlib import Path

from .connections import CONFIG_FILENAME, read_json_or_exit
from .project_config import ENV, MODES, SIMPLE, write_mode
from .util import git_root

MARKER = "managed-by: cimkit-git"

_NON_TTY_WARNING = (
    "cimkit-git: no TTY and no --mode given — defaulting to simple mode "
    "(version control only).\n"
    "  If this project needs connection substitution across deployment targets, "
    "that is environment mode;\n"
    "  re-run with `git cim install --mode env` to opt in."
)

# Probe for a repo-local virtualenv Python, falling back to python3.
_PYTHON_PROBE = """\
REPO_ROOT=$(git rev-parse --show-toplevel)

PYTHON=python3
for candidate in \\
    "$REPO_ROOT/.venv/bin/python3" \\
    "$REPO_ROOT/venv/bin/python3"  \\
    "$REPO_ROOT/env/bin/python3";  \\
do
    if [ -x "$candidate" ]; then PYTHON="$candidate"; break; fi
done
"""


def _hook_script(hook_name: str, blocking: bool, hint: bool = False) -> str:
    """Build a hook script. Blocking hooks fail the git operation on error;
    non-blocking hooks (post-*) never block it. `hint` adds an "is cimkit-git
    installed?" message — useful when failure usually means a missing install
    (pre-commit), but not for pre-push where the command prints its own reason."""
    invoke = f'"$PYTHON" -m cimkit_git hook {hook_name}'
    if not blocking:
        tail = f"{invoke} || true\n"
    elif hint:
        tail = (
            f"{invoke} || {{\n"
            f'    echo "cimkit-git: hook failed — is cimkit-git installed? '
            f'(pip install cimkit-git)" >&2\n'
            f"    exit 1\n"
            f"}}\n"
        )
    else:
        tail = f"{invoke}\n"  # set -e propagates the exit code (and its output)
    return f"#!/usr/bin/env bash\n# {MARKER}\nset -euo pipefail\n\n{_PYTHON_PROBE}\n{tail}"


HOOKS = {
    "pre-commit": _hook_script("pre-commit", blocking=True, hint=True),
    "pre-push": _hook_script("pre-push", blocking=True),
    "post-stash": _hook_script("post-stash", blocking=False),
    "post-merge": _hook_script("post-merge", blocking=False),
    "post-checkout": _hook_script("post-checkout", blocking=False),
}


def install_hooks(repo_root: Path = None) -> None:
    if repo_root is None:
        repo_root = git_root(Path.cwd())

    hooks_dir = repo_root / ".git" / "hooks"
    hooks_dir.mkdir(exist_ok=True)

    for name, content in HOOKS.items():
        hook_path = hooks_dir / name

        if hook_path.exists():
            existing = hook_path.read_text()
            if MARKER in existing:
                # Already installed — overwrite with latest version.
                pass
            else:
                print(
                    f"  cimkit-git: {name} hook already exists and is not ours.\n"
                    f"  Add this line to {hook_path}:\n"
                    f"    python3 -m cimkit_git hook {name}"
                )
                continue

        hook_path.write_text(content)
        hook_path.chmod(hook_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        print(f"  cimkit-git: installed {name} hook")


# --------------------------------------------------------------------------- #
# Mode opt-in — decide and record the Project's Mode in aprx.json (ADR-0001).
# --------------------------------------------------------------------------- #

def _read_config(config_path: Path) -> "tuple[str | None, dict]":
    """Return ``(declared_mode, raw_config)`` for an existing ``aprx.json``.

    A *missing* file is the fresh-install case — ``(None, {})`` — install decides a
    mode and writes a new config. A file that is *present but unreadable / non-UTF-8 /
    malformed / not a JSON object* is **not** collapsed into that same ``(None, {})``:
    doing so would let install silently overwrite a committed config it merely failed
    to *parse*, discarding the developer's ``fields``/``token`` — and you cannot
    preserve fields you cannot read, so the only non-destructive answer is to stop.
    Such a file is a directed ``sys.exit`` instead; install is a user-run one-shot,
    never the fail-open hook sweep, so aborting it crashes no commit — fix or remove
    the file and re-run. The read routes through the shared ``read_json_or_exit`` (the
    single home for the read/decode/parse diagnostics), so install and ``connections
    init`` report a broken ``aprx.json`` identically rather than drifting.

    ``declared_mode`` is ``None`` when the file is absent or present-and-readable but
    carries no recognised ``mode`` — e.g. a legacy mode-less config from ``connections
    init``, whose ``fields``/``token`` come back in ``raw_config`` for the write to
    preserve. A file that already declares a valid mode is honoured untouched."""
    if not config_path.exists():
        return None, {}
    cfg = read_json_or_exit(config_path)
    if not isinstance(cfg, dict):
        sys.exit(f"cimkit-git: {config_path} must be a JSON object (the project config)")
    mode = cfg.get("mode")
    return (mode if mode in MODES else None), cfg


def _prompt_mode(prompt) -> str:
    """Ask the developer to choose a Mode, re-prompting until they pick one.

    Deliberately has no enter-to-default: Mode is the one decision the whole team
    inherits (ADR-0001), so it must be chosen explicitly rather than fallen into."""
    while True:
        try:
            answer = prompt(
                "Project mode?\n"
                "  [simple] version control only\n"
                "  [env]    version control + connection substitution\n"
                "> "
            ).strip().lower()
        except EOFError:
            # stdin closed mid-prompt — abort cleanly rather than tracebacking.
            sys.exit("cimkit-git: no mode selected (end of input) — "
                     "re-run with `git cim install --mode simple|env`")
        if answer in ("simple", "s"):
            return SIMPLE
        if answer in ("env", "e", "environment"):
            return ENV
        print(f"  please answer 'simple' or 'env' (got {answer!r})", file=sys.stderr)


def _decide_mode(mode_flag, interactive: bool, prompt) -> str:
    """Decide a not-yet-recorded Project's Mode.

    Precedence: an explicit ``--mode`` flag wins everywhere; otherwise an
    interactive shell is prompted; a non-interactive shell with no flag falls
    back to simple mode and warns loudly that environment mode exists."""
    if mode_flag is not None:
        return mode_flag
    if interactive:
        return _prompt_mode(prompt)
    print(_NON_TTY_WARNING, file=sys.stderr)
    return SIMPLE


def _write_mode(config_path: Path, existing: dict, mode: str) -> None:
    """Record ``mode`` in ``aprx.json``, preserving any other fields already
    present (e.g. ``fields``/``token`` scaffolded by ``connections init``).

    Delegates to the single ``project_config.write_mode`` serializer so install and
    ``connections init`` always emit the same ``ProjectConfig``-loadable shape."""
    write_mode(config_path, mode, existing)


def install(repo_root: Path = None, config_dir: Path = None, mode: str = None,
            interactive: bool = None, prompt=input) -> str:
    """Record the Project's Mode (if not already declared) and install the hooks.

    An existing ``aprx.json`` with a declared mode is honoured without prompting
    and is **never overwritten** — even an explicit ``--mode`` that conflicts is
    refused rather than diverging the team. Otherwise the mode is taken from
    ``mode`` (the ``--mode`` flag), an interactive prompt, or the non-TTY simple
    default, then written. Returns the effective mode."""
    # Guard the public entry point: argparse's `choices` only covers the CLI path.
    if mode is not None and mode not in MODES:
        sys.exit(f"cimkit-git: unknown mode {mode!r} — "
                 f"expected one of {', '.join(MODES)}")

    config_dir = Path.cwd() if config_dir is None else Path(config_dir)
    config_path = config_dir / CONFIG_FILENAME

    # Resolve (and validate) the repo before writing anything, so a run outside a
    # git repo errors cleanly instead of leaving an orphan aprx.json behind.
    if repo_root is None:
        repo_root = git_root(config_dir)

    declared, existing = _read_config(config_path)

    if declared is not None:
        # A committed team decision already exists (ADR-0001): honour it. A
        # conflicting --mode is refused, not silently applied — the mode is a
        # shared, committed choice, so changing it is a deliberate file edit.
        if mode is not None and mode != declared:
            sys.exit(
                f"cimkit-git: {config_path} already declares mode '{declared}'; "
                f"refusing to overwrite it with '{mode}'. Edit {CONFIG_FILENAME} "
                f"directly if the team is changing modes."
            )
        print(f"  cimkit-git: {CONFIG_FILENAME} already declares mode "
              f"'{declared}' — leaving it unchanged")
        effective = declared
    else:
        if interactive is None:
            interactive = sys.stdin.isatty()
        effective = _decide_mode(mode, interactive, prompt)
        _write_mode(config_path, existing, effective)
        print(f"  cimkit-git: recorded mode '{effective}' in {CONFIG_FILENAME}")

    install_hooks(repo_root)
    return effective
