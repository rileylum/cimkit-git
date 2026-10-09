"""The cimkit-git command. The only module that prints, prompts or exits."""

import argparse
import os
import re
import sys
from collections.abc import Callable
from pathlib import Path

from cimkit_git.errors import CimkitError, PlaceholderError, RefusedError
from cimkit_git.placeholders import Problem
from cimkit_git.sync import Status, Workspace

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


def describe(problem: Problem) -> str:
    if problem.kind == "unregistered":
        return f"{problem.entry}: unregistered value {mask(problem.text)}"
    if problem.kind == "unknown_key":
        return f"{problem.entry}: no key named {problem.text} is declared"
    return f"{problem.entry}: no value is set for {problem.text}"


def each_project(ws: Workspace, projects: list[Path], act: Callable[[Path], str]) -> int:
    """Run act on each project and print what it did; one refusal doesn't stop the rest."""
    code = 0
    unregistered = False
    for binary in projects:
        name = ws.name(binary)
        try:
            print(f"{name}: {act(binary)}")
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

    return each_project(ws, projects, act)


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

    return each_project(ws, projects, act)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cimkit-git")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="print each project's status")
    p = commands.add_parser("sync", help="explode or build, whichever the status calls for")
    p.add_argument("project", nargs="?", type=Path, help="a binary or its Source; all projects if omitted")
    p = commands.add_parser("explode", help="write Source from the binary")
    p.add_argument("project", nargs="?", type=Path, help="a binary or its Source; all projects if omitted")
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
