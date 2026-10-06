"""Environment-aware connection-string substitution.

The committed ``.aprx.src/`` is environment-neutral: configured fields (default
``workspaceConnectionString``) store an ``@@token@@`` placeholder instead of a real
connection string.  The real values live in per-environment connection files.

    explode:  real value  --tokenize-->    @@key@@      (neutral, committed)
    pack:     @@key@@      --substitute-->  real value   (environment-specific build)

Keeping connection strings out of the mergeable content is what lets work flow
dev -> uat -> prd without dragging the wrong database along.

Everything here operates on **parsed JSON objects** rather than text, so values
containing backslashes, semicolons or quotes re-serialise with correct escaping.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

DEFAULT_FIELDS = ("workspaceConnectionString",)
DEFAULT_TOKEN = "@@{key}@@"

CONFIG_FILENAME = "aprx.json"
CONNECTIONS_DIR = "connections"
LOCAL_FILE = "local.json"


# --------------------------------------------------------------------------- #
# Project discovery & config
# --------------------------------------------------------------------------- #

def committed_connection_files(project_dir) -> "list[Path]":
    """The *committed*, team-shared connection files: ``connections/*.json`` (sorted),
    excluding the gitignored per-developer ``local.json``.  The single home for the
    ``connections/*.json`` discovery rule."""
    conn_dir = Path(project_dir) / CONNECTIONS_DIR
    return sorted(conn_dir.glob("*.json")) if conn_dir.is_dir() else []


def resolve_connections_file(project_dir, env=None, connections_file=None) -> "Path | None":
    """Pick the connection file to apply when packing.

    Precedence (highest first):
      1. ``connections_file`` — explicit path, must exist.
      2. ``env``             — ``connections/<env>.json``, must exist.
      3. default             — ``local.json`` if present.

    An explicitly requested file that is missing is a hard error.  When nothing is
    requested and there is no ``local.json``, returns ``None`` (simple mode)."""
    if connections_file:
        p = Path(connections_file)
        if not p.exists():
            sys.exit(f"cimkit-git: connections file {p} not found")
        return p
    if env:
        if project_dir is None:
            sys.exit("cimkit-git: --env requires a project with a connections/ directory")
        p = Path(project_dir) / CONNECTIONS_DIR / f"{env}.json"
        if not p.exists():
            sys.exit(f"cimkit-git: no connections file for environment {env!r} (expected {p})")
        return p
    if project_dir is not None:
        local = Path(project_dir) / LOCAL_FILE
        if local.exists():
            return local
    return None


# --------------------------------------------------------------------------- #
# Connection maps
# --------------------------------------------------------------------------- #

def read_json_or_exit(path):
    """Read and JSON-parse one file, turning the three file-read failure modes into
    three *distinct* ``sys.exit`` diagnostics: an *I/O* failure, a *not-UTF-8* file, and
    *malformed JSON* are different problems a user fixes differently. The **single home**
    for "load a JSON file from disk for this tool, or fail with a directed message" —
    both this module's ``load_connections`` and ``ProjectConfig.load`` route through it
    so ``aprx.json`` and the connection files can never drift apart in how they report
    the same class of failure (issues 0005 and 0009).

    Converting these to ``sys.exit`` (rather than letting them raise) is what lets every
    caller inherit a graceful skip/block: the pre-commit fail-open sweep already catches
    ``SystemExit``, the never-blocking post-* rebuild downgrades it to a skip, and the
    ``verify`` / ``connections check`` gates report it instead of dumping a traceback —
    an uncaught error here would crash the whole commit. ``UnicodeDecodeError`` is a
    ``ValueError``, not an ``OSError``, so it needs its own clause — ``read_text`` raises
    it on a non-UTF-8 file (UTF-16/Latin-1, a stray ``0xFF`` byte), and it would otherwise
    sail past both the ``OSError`` and the ``JSONDecodeError`` guards. Structural
    validation (is it the *right shape* of JSON object) stays with each caller, whose
    "what this file should contain" message differs."""
    path = Path(path)
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError as err:
        sys.exit(
            f"cimkit-git: {path} could not be read ({err}) — "
            f"check the file's permissions and that it is a regular file"
        )
    except UnicodeDecodeError as err:
        sys.exit(f"cimkit-git: {path} is not valid UTF-8 text ({err})")
    try:
        return json.loads(raw)
    except json.JSONDecodeError as err:
        sys.exit(f"cimkit-git: {path} is not valid JSON ({err})")


def load_connections(path) -> "dict[str, str]":
    """Load a ``{key: connection_string}`` JSON object. The read/decode/parse failures
    are converted to directed diagnostics by :func:`read_json_or_exit`; the shape check
    (a JSON *object*) carries the connection-file-specific wording."""
    data = read_json_or_exit(path)
    if not isinstance(data, dict):
        sys.exit(f"cimkit-git: {path} must be a JSON object of key -> connection string")
    return data


def build_reverse_map(files) -> "dict[str, str]":
    """Union of all connection files as ``{connection_string: key}``.

    The same key carrying different values across environments is expected (that is
    the whole point).  The same *value* mapped to two different keys is ambiguous
    and is a hard error."""
    reverse: "dict[str, str]" = {}
    for path in files:
        for key, value in load_connections(path).items():
            existing = reverse.get(value)
            if existing is not None and existing != key:
                sys.exit(
                    f"cimkit-git: connection value {value!r} is mapped to both "
                    f"{existing!r} and {key!r} — a value must map to one key"
                )
            reverse[value] = key
    return reverse


# --------------------------------------------------------------------------- #
# Token <-> value transforms (operate in place on parsed JSON)
# --------------------------------------------------------------------------- #

def _token_regex(token: str) -> "re.Pattern":
    prefix, suffix = token.split("{key}", 1)
    return re.compile("^" + re.escape(prefix) + r"(?P<key>.+?)" + re.escape(suffix) + "$")


def _walk_fields(obj, fields, visit) -> None:
    """The one traversal the four field operations share.

    Descends *obj* depth-first and calls ``visit(node, key, value)`` for every dict
    entry whose key is a configured field carrying a string value — ``node`` is the
    owning dict (so a visitor can rewrite ``node[key]`` in place).  A matched field is
    a leaf: its value is handed to the visitor, not descended into.  Every other
    branch (non-field keys, nested dicts, list items) is recursed.  Tokenize,
    substitute, token scan and value collect differ only in *visit*."""
    fields = set(fields)

    def walk(node):
        if isinstance(node, dict):
            for k, v in node.items():
                if k in fields and isinstance(v, str):
                    visit(node, k, v)
                else:
                    walk(v)
        elif isinstance(node, list):
            for item in node:
                walk(item)

    walk(obj)


def substitute(obj, key_to_value, fields=DEFAULT_FIELDS, token=DEFAULT_TOKEN):
    """Replace ``@@key@@`` tokens in *fields* with their real values.

    Returns ``(obj, missing_keys)``; a token whose key is absent from
    *key_to_value* is collected in ``missing_keys`` (caller fails fast)."""
    regex = _token_regex(token)
    missing: "set[str]" = set()

    def visit(node, k, v):
        m = regex.match(v)
        if m:
            key = m.group("key")
            if key in key_to_value:
                node[k] = key_to_value[key]
            else:
                missing.add(key)
        # a literal (already-real) value is left untouched

    _walk_fields(obj, fields, visit)
    return obj, missing


def tokenize(obj, value_to_key, fields=DEFAULT_FIELDS, token=DEFAULT_TOKEN):
    """Replace real connection strings in *fields* with their ``@@key@@`` token.

    Returns ``(obj, unknown_values)``; a field value that is neither already a token
    nor present in *value_to_key* is collected in ``unknown_values`` (caller fails
    fast — it means an unregistered connection string)."""
    regex = _token_regex(token)
    unknown: "set[str]" = set()

    def visit(node, k, v):
        if regex.match(v):
            return  # already tokenised
        if v in value_to_key:
            node[k] = token.format(key=value_to_key[v])
        else:
            unknown.add(v)

    _walk_fields(obj, fields, visit)
    return obj, unknown


def collect_field_values(obj, fields=DEFAULT_FIELDS) -> "set[str]":
    """All distinct string values found under *fields* — used by ``connections init``
    to discover the connection strings that need keys."""
    found: "set[str]" = set()

    def visit(node, k, v):
        found.add(v)

    _walk_fields(obj, fields, visit)
    return found


def scan_tokens(obj, fields=DEFAULT_FIELDS, token=DEFAULT_TOKEN):
    """Inspect an already-tokenised source. Returns ``(referenced_keys, raw_values)``:
    keys for field values that are ``@@token@@`` placeholders, and raw_values for
    field values that are *not* tokens — a raw value means a real connection string
    leaked into the committed source (e.g. a commit made without the hooks)."""
    regex = _token_regex(token)
    keys: "set[str]" = set()
    raw: "set[str]" = set()

    def visit(node, k, v):
        m = regex.match(v)
        (keys.add(m.group("key")) if m else raw.add(v))

    _walk_fields(obj, fields, visit)
    return keys, raw
