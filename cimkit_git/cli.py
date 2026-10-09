"""The cimkit-git command. The only module that prints, prompts or exits."""

import argparse
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from cimkit_git import checks, config, git
from cimkit_git.errors import CimkitError, ConfigError, PlaceholderError, RefusedError, RegisterError
from cimkit_git.placeholders import Problem
from cimkit_git.register import add_key, set_value, suggest_key
from cimkit_git.sync import Status, Workspace
from cimkit_git.values import load_values

# Until install writes this line, status is the only place that names it.
LINE_ENDINGS_HINT = "Add this line to .gitattributes so git never converts Source:\n    **/*.aprx.src/** -text"


def status(ws: Workspace, args: argparse.Namespace, projects: list[Path]) -> int:
    code = 0
    seen = set()
    for binary in projects:
        try:
            st = ws.status(binary)
        except CimkitError as exc:
            print(f"{ws.name(binary)}: error: {exc}")
            code = 1
            continue
        seen.add(st)
        print(f"{ws.name(binary)}: {st.value}")
    if Status.LINE_ENDINGS in seen:
        print(LINE_ENDINGS_HINT)
    return code


# Connection strings can carry a password, and a refusal is printed to a terminal or CI log.
SECRET = re.compile(r"\b((?:ENCRYPTED_)?PASSWORD=)[^;]*", re.IGNORECASE)

REGISTER_STEPS = """To register a value by hand:
    1. Add a key for it to keys in [git.placeholders] in the config file.
    2. Set that key for every target, in cimkit.local.toml or CIMKIT__<TARGET>__<KEY>.
    3. Run the command again."""

# The way past each refusal. A status a command never refuses on needs no entry.
HINTS = {
    Status.NO_RECORD: "Run explode to keep the binary, or build --discard to keep Source.",
    Status.CONFLICT: "Run explode --force to keep the binary, or build --discard to keep Source.",
    Status.SOURCE_CHANGED: "Run build to keep the Source edit, or explode --force to drop it.",
    Status.BINARY_EDITED: "Run explode to keep the binary edit, or build --discard to drop it.",
    Status.NOT_BUILT: "There is no binary to explode. Run build.",
    Status.NEW_PROJECT: "There is no Source to build. Run explode.",
    Status.UNMERGED: "Resolve the merge in Source, then run the command again.",
    Status.STAGED_EDIT: "The index holds a Source edit that the working copy lacks. Commit, unstage or restore it first.",
    Status.LINE_ENDINGS: LINE_ENDINGS_HINT.replace("\n", "\n    "),
}


def mask(value: str) -> str:
    return SECRET.sub(r"\1***", value)


def explain(problem: Problem | checks.Finding) -> str:
    if problem.kind == "unregistered":
        return f"unregistered value {mask(problem.text)}"
    if problem.kind == "unknown_key":
        return f"no key named {problem.text} is declared"
    if problem.kind == "missing_value":
        return f"no value is set for {problem.text}"
    if problem.kind == "path":
        return f"{problem.count} machine path(s), first {problem.text}"
    return problem.text


def describe(problem: Problem) -> str:
    return f"{problem.entry}: {explain(problem)}"


def interactive(args: argparse.Namespace) -> bool:
    return not args.no_input and sys.stdin.isatty() and sys.stdout.isatty()


def ask(question: str) -> str | None:
    try:
        return input(question).strip()
    except EOFError:
        return None


def choose_key(ws: Workspace, value: str, target: str) -> str | None:
    """The key the user picks for value, or None if they skip."""
    assert ws.config.placeholders and ws.values
    keys = ws.config.placeholders.keys
    suggestion = suggest_key(value, keys)
    while True:
        answer = ask(f"    Key for it in {target}" + (f" [{suggestion}]" if suggestion else "") + ", or s to skip: ")
        if answer is None or answer == "s":
            return None
        key = answer or suggestion
        if not key:
            continue
        if not config.NAME.fullmatch(key):
            print("    A key is lowercase [a-z0-9_].")
        elif key in ws.values.for_target(target):
            print(f"    {key} already has a value in {target}.")
        else:
            return key


def register_values(ws: Workspace, exc: PlaceholderError) -> bool:
    """Prompt to register each unregistered value explode found. True if every one was.

    Reloads the workspace after each one, so the next prompt sees the new key.
    """
    target = exc.target
    unregistered = {p.text: p for p in exc.problems if p.kind == "unregistered"}
    if target is None or not unregistered:
        return False
    # The local file holds the values, passwords included, so it must never be committed.
    if not git.ignored(ws.root, ws.local_path):
        print(f"Add {ws.local_path.name} to .gitignore before registering a value; it can hold passwords.")
        return False
    for value, problem in unregistered.items():
        assert ws.config.placeholders
        print(describe(problem))
        key = choose_key(ws, value, target)
        if key is None:
            return False
        try:
            if key not in ws.config.placeholders.keys:
                add_key(ws.config.path, key)
            set_value(ws.local_path, target, key, value)
        except RegisterError as err:
            print(f"    {err}")
            return False
        ws.reload()
        assert ws.values
        lacking = [t for t in ws.config.placeholders.targets if key not in ws.values.for_target(t)]
        print(f"    Registered {key} in {target}." + (f" Set it for {', '.join(lacking)} too." if lacking else ""))
    return True


def each_project(ws: Workspace, projects: list[Path], act: Callable[[Path], str], prompt: bool = False) -> int:
    """Run act on each project and print what it did; one refusal doesn't stop the rest.

    With prompt, an explode that finds unregistered values offers to register them, then
    runs once more.
    """

    def attempt(binary: Path) -> str:
        try:
            return act(binary)
        except PlaceholderError as exc:
            if not (prompt and register_values(ws, exc)):
                raise
        return act(binary)

    code = 0
    unregistered = False
    for binary in projects:
        name = ws.name(binary)
        try:
            print(f"{name}: {attempt(binary)}")
            continue
        except PlaceholderError as exc:
            print(f"{name}: refused: placeholder problems")
            for problem in exc.problems:
                print(f"    {describe(problem)}")
            unregistered |= any(p.kind == "unregistered" for p in exc.problems)
        except RefusedError as exc:
            print(f"{name}: refused: {exc}")
            print(f"    {HINTS[exc.status]}")
        except CimkitError as exc:
            print(f"{name}: error: {exc}")
        code = 1
    if unregistered:
        print(REGISTER_STEPS)
    return code


def target_note(target: str | None) -> str:
    return f", target {target}" if target else ""


def explode(ws: Workspace, args: argparse.Namespace, projects: list[Path]) -> int:
    def act(binary: Path) -> str:
        return "exploded" + target_note(ws.explode_project(binary, force=args.force, target=args.target).target)

    return each_project(ws, projects, act, interactive(args))


def build(ws: Workspace, args: argparse.Namespace, projects: list[Path]) -> int:
    def act(binary: Path) -> str:
        if args.output:
            return f"built {args.output}" + target_note(ws.build_artifact(binary, args.output, target=args.target))
        return "built" + target_note(ws.build_project(binary, target=args.target, discard=args.discard).target)

    return each_project(ws, projects, act)


def sync(ws: Workspace, args: argparse.Namespace, projects: list[Path]) -> int:
    def act(binary: Path) -> str:
        done, record = ws.sync_project(binary)
        return done + target_note(record.target if record else None)

    return each_project(ws, projects, act, interactive(args))


def check(args: argparse.Namespace) -> int:
    """Takes no lock and reads no sync state: it reads only what is committed."""
    start = Path.cwd().resolve()
    committed = checks.at_rev(start, args.rev) if args.rev else checks.working_tree(start)
    cfg = committed.config
    errors: list[str] = []
    warnings: list[str] = []
    if args.target:
        if not cfg.placeholders:
            raise ConfigError(f"{cfg.path} declares no targets")
        # Loaded only here: without a target, check needs no values and so no secrets.
        values = load_values(cfg.placeholders, cfg.path.parent / "cimkit.local.toml", os.environ)
        rel = cfg.path.relative_to(committed.root).as_posix()
        errors += [f"{rel}: error: {args.target} has no value for {key}" for key in values.missing(args.target)]
    for name, read in committed.sources.items():
        try:
            findings = checks.check(read(), cfg.placeholders)
        except CimkitError as exc:
            errors.append(f"{name}: error: {exc}")
            continue
        for finding in findings:
            if finding.kind == "path":
                warnings.append(f"{name}/{finding.entry}: warning: {explain(finding)}")
            else:
                errors.append(f"{name}/{finding.entry}: error: {explain(finding)}")
    # Errors first: path warnings fire on nearly every project and would bury them.
    for line in errors + warnings:
        print(line)
    print(f"{len(errors)} error(s), {len(warnings)} warning(s)")
    return 1 if errors else 0


NO_INPUT = "never prompt to register a value; print the steps instead"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cimkit-git")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="print each project's status")
    p = commands.add_parser("check", help="the CI gate: check committed Source for leaks and parse errors")
    p.add_argument("--target", help="also fail if this target lacks a value for a declared key")
    p.add_argument("--rev", help="check this commit instead of the working tree")
    p = commands.add_parser("sync", help="explode or build, whichever the status calls for")
    p.add_argument("project", nargs="?", type=Path, help="a binary or its Source; all projects if omitted")
    p.add_argument("--no-input", action="store_true", help=NO_INPUT)
    p = commands.add_parser("explode", help="write Source from the binary")
    p.add_argument("project", nargs="?", type=Path, help="a binary or its Source; all projects if omitted")
    p.add_argument("--no-input", action="store_true", help=NO_INPUT)
    p.add_argument("--force", action="store_true", help="overwrite Source edits made since the last sync")
    p.add_argument("--target", help="the target to record, which the next sync builds for")
    p = commands.add_parser("build", help="write the binary from Source")
    p.add_argument("project", nargs="?", type=Path, help="a binary or its Source; all projects if omitted")
    p.add_argument("--target", help="the target to build for; defaults to the recorded one")
    p.add_argument("--discard", action="store_true", help="overwrite binary edits made since the last sync")
    p.add_argument("-o", "--output", type=Path, help="write a deploy artifact here instead of the working binary")
    args = parser.parse_args(argv)
    output = getattr(args, "output", None)
    if output and not args.project:
        parser.error("build -o needs a project")
    try:
        if args.command == "check":
            return check(args)
        ws = Workspace(Path.cwd(), os.environ)
        named = getattr(args, "project", None)
        projects = [ws.project(named)] if named else ws.projects()
        run = {"status": status, "sync": sync, "explode": explode, "build": build}[args.command]
        # status and build -o change no binary, Source or record, so they run beside a write.
        if args.command == "status" or output:
            return run(ws, args, projects)
        with ws.locked():
            return run(ws, args, projects)
    except CimkitError as exc:
        print(f"cimkit-git: {exc}", file=sys.stderr)
        return 1
