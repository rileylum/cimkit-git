"""`aprx connections init` / `aprx connections check` — scaffold and validate the
per-environment connection files."""

import json
import re
import sys
from pathlib import Path

from . import connections as conn
from .entry import parsed_json_entries
from .project_config import ENV, SIMPLE, ProjectConfig, write_mode


def _suggest_key(value: str, taken: set) -> str:
    """Derive a short, stable key from a connection string.

    Prefers a .gdb / .sde basename, then a DATABASE=... value, falling back to
    conn1, conn2, ... — always disambiguated against keys already taken."""
    m = re.search(r"([^\\/=;]+)\.(?:gdb|sde)", value, re.IGNORECASE)
    if not m:
        m = re.search(r"DATABASE=([^;]+)", value, re.IGNORECASE)
    base = m.group(1) if m else ""
    base = re.sub(r"[^0-9a-zA-Z]+", "_", base.split("\\")[-1].split("/")[-1]).strip("_").lower()

    if not base:
        i = 1
        while f"conn{i}" in taken:
            i += 1
        return f"conn{i}"

    key, i = base, 2
    while key in taken:
        key, i = f"{base}_{i}", i + 1
    return key


def _write_json(path: Path, label: str, data: dict) -> bool:
    if path.exists():
        print(f"  exists, leaving as-is: {label}")
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    print(f"  wrote {label}")
    return True


def _init_fields_token(project: Path, existing: dict) -> "tuple[list, str]":
    """The ``fields``/``token`` ``connections init`` should scan and record.

    ``init`` is what first declares a Project's mode, so it can run *before* any
    ``aprx.json`` exists — there is no ``ProjectConfig`` to resolve yet, so it falls
    back to any fields/token the file already carries (a legacy mode-less config) or
    the engine defaults. But when a mode is already declared (``aprx install --mode
    env`` ran first), ``ProjectConfig`` is the source of ``fields``/``token`` rather
    than a re-derived copy. A committed ``mode: simple`` is a deliberate team
    decision; ``init`` refuses to silently switch it to ``env``."""
    declared = existing.get("mode")
    if declared == SIMPLE:
        sys.exit(
            f"aprx-tools: {project / conn.CONFIG_FILENAME} already declares "
            f"mode 'simple'; connections init would switch it to 'env'.\n"
            f"  Edit aprx.json's \"mode\" to \"env\" to upgrade, then re-run."
        )
    if declared is None:
        # No mode yet — init is declaring it. Preserve any fields/token already in
        # the file (a legacy aprx.json the previous `init` scaffolded with no mode,
        # or a hand-written custom token), falling back to the engine defaults.
        return (list(existing.get("fields", conn.DEFAULT_FIELDS)),
                existing.get("token", conn.DEFAULT_TOKEN))
    # Mode already declared (env, or an unknown value): let ProjectConfig resolve
    # and validate it — the single home for the fields/token reading ritual.
    cfg = ProjectConfig.load(project)
    return list(cfg.fields), cfg.token


def connections_init(aprx_file: str) -> None:
    aprx = Path(aprx_file)
    if not aprx.exists():
        sys.exit(f"aprx-tools: {aprx} not found")

    # init *establishes* the project at the .aprx's directory; it cannot resolve a
    # ProjectConfig for a project it is about to create, so it reads aprx.json raw
    # (if any) to source fields/token and to refuse a conflicting declared mode.
    project = aprx.parent
    config_path = project / conn.CONFIG_FILENAME
    # init is a user-invoked one-shot, so an unreadable / non-UTF-8 / malformed aprx.json
    # should fail with the directed read_json_or_exit diagnostic (the same posture
    # `connections check` inherits) rather than a raw traceback. The exists() short-circuit
    # stays: a missing aprx.json is the normal fresh-init case ({}), not an error. The
    # isinstance guard pairs with read_json_or_exit exactly as load_connections does:
    # read_json_or_exit covers read/decode/parse, the shape check covers valid-JSON-that-
    # is-not-an-object ([], null, a bare string) so `existing.get("mode")` below can't
    # blow up with a raw AttributeError.
    existing = conn.read_json_or_exit(config_path) if config_path.exists() else {}
    if not isinstance(existing, dict):
        sys.exit(f"aprx-tools: {config_path} must be a JSON object (the project config)")
    fields, token = _init_fields_token(project, existing)

    # The distinct connection strings to scaffold keys for. The field-walk lives behind
    # ProjectConfig (issue 0003): init resolves the fields/token it is about to declare,
    # then asks that same config object what values the binary holds — fed the parsed
    # JSON entries from the Entry reader (issue 0002), never a hand-rolled zip walk.
    cfg = ProjectConfig(dir=project, mode=ENV, fields=tuple(fields), token=token)
    values = cfg.discovered_values(entry.parsed for entry in parsed_json_entries(aprx))
    if not values:
        sys.exit(f"aprx-tools: no connection strings found in fields {fields} — "
                 f"nothing to scaffold")

    mapping: dict = {}
    for value in sorted(values):
        mapping[_suggest_key(value, set(mapping))] = value

    print(f"Found {len(mapping)} distinct connection string(s):")
    for key, value in mapping.items():
        print(f"  {key} = {value}")

    # aprx.json — declare environment mode and record the resolved fields/token,
    # preserving any other keys already present. Funnels through the same writer as
    # `aprx install` (project_config.write_mode, which drops the old mode) so the two
    # never emit divergent shapes; the explicit fields/token override whatever the
    # file held with the values we just resolved/scanned against.
    write_mode(config_path, ENV, {**existing, "fields": fields, "token": token})
    print(f"  wrote {conn.CONFIG_FILENAME} (mode: env)")
    # connections/dev.json — real values discovered from the .aprx
    _write_json(project / conn.CONNECTIONS_DIR / "dev.json",
                f"{conn.CONNECTIONS_DIR}/dev.json", mapping)
    # local.json.example — template a developer copies to local.json
    _write_json(project / "local.json.example", "local.json.example", mapping)

    print("\nNext steps:")
    print("  1. Add to .gitignore:")
    print("       local.json")
    print("       *.aprx")
    print("  2. cp local.json.example local.json   # then fill in your local paths")
    print("  3. Create connections/uat.json, connections/prd.json with the same keys.")
    print("  4. aprx explode <file>.aprx           # connection strings become @@tokens@@")


def connections_check() -> None:
    # Resolve the project at the current directory and route through ProjectConfig:
    # ProjectConfig.load owns the strict "no aprx.json / no declared mode → run aprx
    # install" error (ADR-0001), and committed_connection_files owns the env-only
    # discovery, so check never re-derives the resolution ritual or silently runs
    # against a simple-mode project. The project dir is cwd — the same declared-mode
    # paradigm the rest of the CLI uses, not a presence-sniffing walk-up.
    cfg = ProjectConfig.load(Path.cwd())
    key_sets = cfg.connection_key_sets()  # {filename: {keys}}, env-only discovery via cfg
    if not key_sets:
        sys.exit(f"aprx-tools: no connection files in {cfg.dir / conn.CONNECTIONS_DIR}")

    all_keys: set = set().union(*key_sets.values())

    ok = True
    for name, keys in key_sets.items():
        gaps = all_keys - keys
        if gaps:
            ok = False
            print(f"  {name}: missing {', '.join(sorted(gaps))}")

    if ok:
        print(f"OK — {len(key_sets)} environment(s) define the same {len(all_keys)} key(s).")
    sys.exit(0 if ok else 1)
