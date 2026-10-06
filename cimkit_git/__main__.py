import argparse
import sys

from . import __version__


def main() -> None:
    parser = argparse.ArgumentParser(
        prog="git cim",
        description="Version-control tooling for ArcGIS .aprx project files.",
    )
    parser.add_argument("--version", action="version", version=f"cimkit-git {__version__}")
    sub = parser.add_subparsers(dest="command", metavar="<command>")

    # explode
    p = sub.add_parser("explode", help="Extract an .aprx into a diffable directory.")
    p.add_argument("aprx_file", metavar="file.aprx")
    p.add_argument("output_dir", metavar="output_dir", nargs="?",
                   help="Destination directory (default: <file>.aprx.src)")

    # pack
    p = sub.add_parser("pack", help="Pack a directory back into an .aprx file.")
    p.add_argument("src_dir", metavar="dir")
    p.add_argument("output_file", metavar="output.aprx", nargs="?",
                   help="Destination file (default: derived from directory name)")
    p.add_argument("--env", metavar="NAME",
                   help="Build for environment NAME (uses connections/NAME.json).")
    p.add_argument("--connections", metavar="FILE", dest="connections_file",
                   help="Explicit connections file (overrides --env and the "
                        "local.json default).")

    # build
    p = sub.add_parser("build", help="(Re)build the working .aprx from its src dir(s).")
    p.add_argument("src_dir", metavar="dir", nargs="?",
                   help="Specific .aprx.src directory (default: every one in the repo).")
    p.add_argument("--env", metavar="NAME",
                   help="Environment to build for (default: local.json).")

    # compare
    p = sub.add_parser("compare", help="Semantically diff two .aprx files or directories.")
    p.add_argument("a", metavar="a")
    p.add_argument("b", metavar="b")

    # verify
    p = sub.add_parser("verify", help="CI gate: source is tokenised and builds for every env.")
    p.add_argument("src_dir", metavar="dir", nargs="?",
                   help="Specific .aprx.src directory (default: every one in the repo).")
    p.add_argument("--env", metavar="NAME",
                   help="Verify only this environment (default: all connections/*.json).")

    # connections
    p = sub.add_parser("connections", help="Manage per-environment connection files.")
    csub = p.add_subparsers(dest="connections_command", metavar="<subcommand>")
    pi = csub.add_parser("init", help="Scaffold connection files from an existing .aprx.")
    pi.add_argument("aprx_file", metavar="file.aprx")
    csub.add_parser("check", help="Verify every environment defines the same keys.")

    # install
    p = sub.add_parser("install", help="Install git hooks into the current repository.")
    p.add_argument("--mode", choices=["simple", "env"],
                   help="Record the project mode without prompting (simple = version "
                        "control only; env = version control + connection substitution).")

    # hook (internal — called by the installed hook scripts)
    p = sub.add_parser("hook", help=argparse.SUPPRESS)
    p.add_argument("hook_name",
                   choices=["pre-commit", "pre-push", "post-stash",
                            "post-merge", "post-checkout"])

    args = parser.parse_args()

    if args.command == "explode":
        # Composition root: resolve the Project's declared mode and inject the
        # matching transform. explode itself is connection-ignorant (ADR-0002) — the
        # choice of IDENTITY (simple) vs Substitution (env) is made here, once.
        from pathlib import Path
        from .explode import explode
        from .transform import explode_transform, SubstitutionError

        aprx = Path(args.aprx_file)
        if not aprx.exists():
            sys.exit(f"cimkit-git: {aprx} not found")
        transform = explode_transform(aprx.resolve().parent)
        try:
            explode(args.aprx_file, args.output_dir, transform=transform)
        except SubstitutionError as e:
            sys.exit(str(e))

    elif args.command == "pack":
        # Composition root: resolve the Project's declared mode and inject the matching
        # pack transform. pack itself is connection-ignorant (ADR-0002) — IDENTITY for
        # simple mode, Substitution for env mode. The env-selection flags only apply in
        # env mode; pack_transform rejects them on a simple-mode project.
        from pathlib import Path
        from .pack import pack
        from .transform import pack_transform, SubstitutionError

        src = Path(args.src_dir)
        if not src.is_dir():
            sys.exit(f"cimkit-git: {src} is not a directory")
        transform = pack_transform(src.resolve().parent,
                                   env=args.env, connections_file=args.connections_file)
        try:
            pack(args.src_dir, args.output_file, transform=transform)
        except SubstitutionError as e:
            sys.exit(str(e))

    elif args.command == "build":
        from .hooks import build_working_copies
        build_working_copies(src_dir=args.src_dir, env=args.env)

    elif args.command == "verify":
        from .verify import verify
        sys.exit(verify(args.src_dir, args.env))

    elif args.command == "compare":
        from .compare import compare
        has_diff = compare(args.a, args.b)
        if not has_diff:
            print("Identical: all files match semantically.")
        sys.exit(1 if has_diff else 0)

    elif args.command == "connections":
        from .bootstrap import connections_init, connections_check
        if args.connections_command == "init":
            connections_init(args.aprx_file)
        elif args.connections_command == "check":
            connections_check()
        else:
            p.print_help()
            sys.exit(1)

    elif args.command == "install":
        from .install import install
        install(mode=args.mode)

    elif args.command == "hook":
        from . import hooks
        sys.exit({
            "pre-commit": hooks.hook_pre_commit,
            "pre-push": hooks.hook_pre_push,
            "post-stash": hooks.hook_post_stash,
            "post-merge": hooks.hook_post_merge,
            "post-checkout": hooks.hook_post_checkout,
        }[args.hook_name]() or 0)

    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
