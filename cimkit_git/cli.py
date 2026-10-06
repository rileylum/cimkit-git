"""The cimkit-git command. The only module that prints, prompts or exits."""

import argparse
import os
import sys
from pathlib import Path

from cimkit_git.errors import CimkitError
from cimkit_git.sync import Status, Workspace

# Until install writes this line, status is the only place that names it.
LINE_ENDINGS_HINT = "Add this line to .gitattributes so git never converts Source:\n    **/*.aprx.src/** -text"


def status(ws: Workspace) -> int:
    code = 0
    seen = set()
    for binary in ws.projects():
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="cimkit-git")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("status", help="print each project's status")
    parser.parse_args(argv)
    try:
        ws = Workspace(Path.cwd(), os.environ)
    except CimkitError as exc:
        print(f"cimkit-git: {exc}", file=sys.stderr)
        return 1
    return status(ws)
