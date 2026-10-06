"""``ProjectConfig`` — the single home for a Project's resolution ritual.

A Project declares its **Mode** once, explicitly, in a committed ``aprx.json``
(``"mode": "simple" | "env"``, alongside ``fields`` and ``token``). ``ProjectConfig``
is the one object that reads that declaration and, for environment-mode Projects,
discovers the connection files and builds the token<->value maps. Every caller that
needs to know "what is this Project and how does it substitute" goes through here, so
the assembly sequence is not copy-pasted across explode / pack / verify / bootstrap.

Resolution is **strict** (ADR-0001): a Project with no ``aprx.json``, or one whose
``aprx.json`` omits ``mode``, is a hard error directing the user to run ``git cim install``.
The old presence-sniffing heuristic (infer env mode from a stray ``connections/`` dir or
``local.json``) is gone — mode is read, never guessed.

Named ``ProjectConfig`` and **not** ``Project``: "Project" is the domain noun for the
ArcGIS project itself (see ``docs/agent/CONTEXT.md``).
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass
from pathlib import Path

from . import connections as conn

SIMPLE = "simple"
ENV = "env"
MODES = (SIMPLE, ENV)

_INSTALL_HINT = "run `git cim install` to declare it"


def write_mode(config_path, mode: str, existing: "dict | None" = None) -> None:
    """Write *mode* into an ``aprx.json``, ``mode`` first, preserving any other keys
    in *existing* (e.g. the ``fields``/``token`` scaffolded by ``connections init``).

    This is the **single writer** of the ``ProjectConfig``-loadable shape. Both
    ``git cim install`` and ``connections init`` funnel through it so the two paths can
    never emit divergent files: whichever runs second keeps what the first wrote."""
    merged = {"mode": mode}
    merged.update({k: v for k, v in (existing or {}).items() if k != "mode"})
    Path(config_path).write_text(json.dumps(merged, indent=2) + "\n", encoding="utf-8")


@dataclass(frozen=True)
class ProjectConfig:
    """A Project's declared configuration, loaded from its ``aprx.json``.

    Attributes:
        dir:    the Project directory (the one holding ``aprx.json``).
        mode:   ``"simple"`` or ``"env"``.
        fields: the JSON field names whose values are connection strings.
        token:  the placeholder format, e.g. ``"@@{key}@@"``.
        commit_binary: the author's binary-lifecycle policy (issue 0003), a
            **tri-state**: ``None`` (absent — no declared policy, the lenient Part 1
            behaviour), ``True`` (commit it — ``verify`` requires a present, in-sync
            binary), or ``False`` (declared no-commit). ``None`` is deliberately *not*
            ``False``: an absent key must change nothing, which is what keeps every
            pre-existing Project backward-compatible.
    """

    dir: Path
    mode: str
    fields: tuple[str, ...]
    token: str
    commit_binary: bool | None = None

    # ----------------------------------------------------------------- #
    # Construction — the one place the file is read and validated.
    # ----------------------------------------------------------------- #

    @classmethod
    def load(cls, project_dir) -> "ProjectConfig":
        """Read and validate ``<project_dir>/aprx.json``.

        Strict: a missing file or a missing ``mode`` is a hard error pointing at
        ``git cim install``. Returns a frozen ``ProjectConfig`` on success."""
        project_dir = Path(project_dir)
        cfg_path = project_dir / conn.CONFIG_FILENAME

        if not cfg_path.exists():
            sys.exit(
                f"cimkit-git: {project_dir} has no {conn.CONFIG_FILENAME} — "
                f"this project has no declared mode; {_INSTALL_HINT}"
            )

        # Read/decode/parse failures become three distinct diagnostics (issue 0005) via
        # the shared loader (issue 0009) — an *I/O* failure, a *not-UTF-8* file, and
        # *malformed JSON* are different problems the user fixes differently. Routing both
        # aprx.json and the connection files through conn.read_json_or_exit keeps that
        # wording in one home, so the two read-sites can never drift apart. The existence
        # check above stays here: a *missing* aprx.json is not an I/O error but the
        # "this project declared no mode — run git cim install" case, with its own message.
        cfg = conn.read_json_or_exit(cfg_path)
        if not isinstance(cfg, dict):
            sys.exit(f"cimkit-git: {cfg_path} must be a JSON object — {_INSTALL_HINT}")

        if "mode" not in cfg:
            sys.exit(
                f"cimkit-git: {cfg_path} declares no 'mode' — {_INSTALL_HINT}"
            )

        mode = cfg["mode"]
        if mode not in MODES:
            sys.exit(
                f"cimkit-git: {cfg_path} has unknown mode {mode!r} — "
                f"expected one of {', '.join(MODES)}"
            )

        token = cfg.get("token", conn.DEFAULT_TOKEN)
        if "{key}" not in token:
            sys.exit(f"cimkit-git: token format {token!r} must contain '{{key}}'")

        fields = cfg.get("fields", conn.DEFAULT_FIELDS)
        # A bare string would be shredded into characters by tuple(), silently
        # matching no field and leaking raw connection strings — reject it.
        if isinstance(fields, str) or not isinstance(fields, (list, tuple)):
            sys.exit(
                f"cimkit-git: 'fields' in {cfg_path} must be a list of field names"
            )

        # Binary-lifecycle policy (issue 0003). Absent => None (lenient, no declared
        # policy) — distinct from a deliberate `false`. `bool` is checked exactly (not
        # truthiness) so a typo'd `1`/`"true"` is rejected here rather than silently
        # coerced into a policy the author did not write. `isinstance(True, int)` holds
        # in Python, so the `bool` test must precede any int-friendliness.
        commit_binary = cfg.get("commit_binary")
        if commit_binary is not None and not isinstance(commit_binary, bool):
            sys.exit(
                f"cimkit-git: 'commit_binary' in {cfg_path} must be true or false"
            )

        return cls(
            dir=project_dir,
            mode=mode,
            fields=tuple(fields),
            token=token,
            commit_binary=commit_binary,
        )

    # ----------------------------------------------------------------- #
    # Mode predicate
    # ----------------------------------------------------------------- #

    @property
    def is_env(self) -> bool:
        """True for environment-mode Projects (the ones that substitute)."""
        return self.mode == ENV

    # ----------------------------------------------------------------- #
    # Environment mode — connection discovery & map building.
    # Built lazily: connection files may not exist yet at load time, and
    # explode (reverse) vs pack (forward) want different maps.
    # ----------------------------------------------------------------- #

    def _require_env(self, what) -> None:
        """Mode is the master switch: substitution is meaningless in simple mode,
        so calling an env-only helper there is a hard error rather than a silent
        no-op that might pick up a stray ``local.json``."""
        if not self.is_env:
            sys.exit(
                f"cimkit-git: {self.dir} is a simple-mode project — "
                f"{what} is only available in environment mode"
            )

    def committed_connection_files(self) -> "list[Path]":
        """Only ``connections/*.json`` — the **committed**, team-shared environments,
        excluding the gitignored, per-developer ``local.json``."""
        self._require_env("committed connection-file discovery")
        return conn.committed_connection_files(self.dir)

    def committed_reverse_map(self) -> "dict[str, str]":
        """``{connection_string: key}`` unioned across the **committed** environments
        only (``connections/*.json``, never ``local.json``) — used to **tokenize** on
        explode.

        Tokenising against committed files alone is a safety property: a connection
        string that exists only in a developer's ``local.json`` is an *unregistered*
        value, so it surfaces as an explode error instead of silently tokenising into
        committed source that no teammate's environment can build. Environment mode
        with no committed connection file is a hard error here — there is nothing to
        tokenize against, so every real connection string would otherwise be reported
        as 'unregistered' one-by-one rather than with one clear message."""
        self._require_env("the committed connection reverse map")
        files = self.committed_connection_files()
        if not files:
            sys.exit(
                f"cimkit-git: {self.dir} is an environment-mode project but has no "
                f"{conn.CONNECTIONS_DIR}/*.json to tokenize against — "
                f"run `git cim connections init` or add a connections file"
            )
        return conn.build_reverse_map(files)

    def forward_map(self, env=None, connections_file=None) -> "dict[str, str]":
        """``{key: connection_string}`` for one chosen environment — used to
        **substitute** on pack. Precedence: ``connections_file`` > ``env`` >
        ``local.json`` (see ``connections.resolve_connections_file``). Errors if
        nothing resolves, so pack never emits a Project full of bare tokens."""
        self._require_env("the connection forward map")
        path = conn.resolve_connections_file(self.dir, env, connections_file)
        if path is None:
            sys.exit(
                f"cimkit-git: {self.dir} has no connection values to pack with "
                f"(no --connections, no --env, no {conn.LOCAL_FILE})"
            )
        return conn.load_connections(path)

    def connection_key_sets(self) -> "dict[str, set[str]]":
        """``{filename: {keys}}`` for each **committed** environment — feeds
        ``connections check``'s "every environment defines the same keys" assertion
        without the caller reaching into ``load_connections`` itself."""
        self._require_env("connection key-set inspection")
        return {
            f.name: set(conn.load_connections(f))
            for f in self.committed_connection_files()
        }

    # ----------------------------------------------------------------- #
    # Environment mode — the domain questions over a Project's entries.
    #
    # These answer "is this Source neutral, and does each environment build
    # it?" — the questions verify and bootstrap used to hand-assemble from the
    # low-level connections engine (``scan_tokens`` / ``collect_field_values`` +
    # set arithmetic, issue 0003). They take the **parsed JSON entries** the
    # Entry reader yields (``entry.parsed`` values, issue 0002) rather than a
    # path, so the field-walk lives behind this one object and the questions
    # unit-test by passing a list of parsed dicts — no ``.aprx``, no directory
    # walk. Pass a re-iterable sequence (a list) when more than one question
    # scans the same Source, or a spent generator answers the later ones empty.
    # ----------------------------------------------------------------- #

    def referenced_keys(self, parsed_entries) -> "set[str]":
        """The token keys the Source references: every ``@@key@@`` placeholder found
        in a configured field across *parsed_entries*."""
        keys: "set[str]" = set()
        for parsed in parsed_entries:
            found, _ = conn.scan_tokens(parsed, self.fields, self.token)
            keys |= found
        return keys

    def leaked_values(self, parsed_entries) -> "set[str]":
        """Raw connection strings that leaked into the meant-to-be-neutral Source:
        configured-field values that are *not* tokens. A non-empty result means a
        commit was made without the hooks; verify turns it into a failure."""
        raw: "set[str]" = set()
        for parsed in parsed_entries:
            _, found = conn.scan_tokens(parsed, self.fields, self.token)
            raw |= found
        return raw

    def unresolved_keys(self, parsed_entries, env_file) -> "set[str]":
        """The referenced keys *env_file* does not define — the "does this
        environment cover every token the Source uses" check. Empty means the
        Project builds for that environment."""
        return self.referenced_keys(parsed_entries) - set(conn.load_connections(env_file))

    def discovered_values(self, parsed_entries) -> "set[str]":
        """Every distinct connection string under the configured fields — what
        ``connections init`` scans a fresh Project's binary for to scaffold the
        per-environment connection files."""
        values: "set[str]" = set()
        for parsed in parsed_entries:
            values |= conn.collect_field_values(parsed, self.fields)
        return values
