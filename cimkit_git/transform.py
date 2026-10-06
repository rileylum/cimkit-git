"""The explode/pack transform seam — environment-mode ``Substitution`` adapter.

``explode`` and ``pack`` are a pure version-control core that knows nothing about
connections (ADR-0002). They accept a **transform** and apply it to every parsed JSON
entry. A transform is **two-phase**:

    apply(parsed)        -- mutate one parsed entry in place, remembering any problems
    raise_if_problems()  -- called once after every entry is computed and *before* any
                            output is written; raises a typed error listing all problems

Splitting the work this way preserves the core's compute-then-write guarantee: a bad
*entry* aborts the whole operation before the filesystem is touched, and the user is
shown *every* offender at once rather than failing on the first.

The two phases cover **per-entry** problems — an unregistered connection string on
explode, a missing token key on pack — the things only discoverable by walking each
entry. Whole-Project **preconditions** (a malformed ``aprx.json``, a value mapped to two
keys, a chosen environment file that doesn't exist) are resolved earlier, when the
factory asks its ``ProjectConfig`` for the map, and fail fast there via ``ProjectConfig``'s
own hard errors — there is nothing to accumulate before a usable map exists.

``Substitution`` is the environment-mode transform, built from a ``ProjectConfig``:

    Substitution.for_explode(cfg)            -- tokenize: connection string -> @@token@@
    Substitution.for_pack(cfg, env=...)      -- substitute: @@token@@ -> connection string

It is a thin adapter over the pure engine in :mod:`cimkit_git.connections`, which already
operates on parsed JSON (never text) so values containing backslashes, semicolons or
quotes re-serialise with correct escaping. ``Substitution`` adds three things: a factory
that picks the direction, a map sourced from ``ProjectConfig``, and ownership of the
error wording — keeping that wording out of the connection-ignorant core.

The simple-mode counterpart is ``IDENTITY``: a no-op transform whose ``apply`` passes
the entry through untouched and whose ``raise_if_problems`` never raises. It is what makes
the seam *real* — the core can be exercised end-to-end with zero connection setup — and it
is the transform the composition root injects when a Project is in simple mode.
"""

from __future__ import annotations

import sys

from . import connections as conn


class _Identity:
    """The simple-mode no-op transform (the singleton :data:`IDENTITY`).

    Version control with nothing swapped: ``apply`` leaves every entry exactly as parsed
    and ``raise_if_problems`` has nothing to report. Stateless, so a single shared
    instance serves every explode/pack."""

    def apply(self, parsed):
        """Pass the parsed entry through untouched."""
        return parsed

    def raise_if_problems(self):
        """No-op: a faithful render can never fail."""


#: The simple-mode transform — inject this when a Project declares ``mode: simple``.
IDENTITY = _Identity()


def explode_transform(project_dir):
    """Composition-root helper for the **explode** side of the seam.

    Load a Project's declared mode from ``project_dir`` and return the transform to
    inject: ``IDENTITY`` for simple mode, ``Substitution.for_explode`` for environment
    mode. The mode-selection branch lives here, in exactly one place, so the CLI
    dispatch and the test harness resolve a transform identically and can never drift
    (a simple project misrouted to ``Substitution``, or vice versa, would be a silent
    leak). Imported lazily to avoid a config<->transform import cycle."""
    from .project_config import ProjectConfig

    cfg = ProjectConfig.load(project_dir)
    return Substitution.for_explode(cfg) if cfg.is_env else IDENTITY


def pack_transform(project_dir, env=None, connections_file=None):
    """Composition-root helper for the **pack** side of the seam.

    The mirror of :func:`explode_transform`. Load a Project's declared mode and return
    the transform to inject: ``Substitution.for_pack`` for environment mode, ``IDENTITY``
    for simple mode. Mode is the master switch (ADR-0001): the environment-selection
    flags (``--env`` / ``--connections``) only mean something in environment mode, so
    passing one to a simple-mode Project is a hard error here rather than a silently
    ignored no-op. Keeping the branch in this one place lets the CLI dispatch and the
    test harness resolve the pack transform identically and never drift. Imported lazily
    to avoid a config<->transform import cycle.

    A ``Substitution.for_pack`` whose chosen environment resolves to nothing aborts at
    construction (``ProjectConfig.forward_map`` errors), so an environment-mode pack can
    never emit a Project full of unsubstituted tokens."""
    from .project_config import ProjectConfig

    cfg = ProjectConfig.load(project_dir)
    if cfg.is_env:
        return Substitution.for_pack(cfg, env, connections_file)
    if env or connections_file:
        sys.exit(
            f"cimkit-git: {cfg.dir} is not an environment-mode project — "
            f"--env / --connections do not apply to a simple-mode project"
        )
    return IDENTITY


class SubstitutionError(Exception):
    """Raised by :meth:`Substitution.raise_if_problems` when one or more entries could
    not be fully transformed — unregistered connection strings on explode, or missing
    token keys on pack. Carries every offender so they are fixed in one pass."""


def _bullet_list(items) -> str:
    """One offender per line — connection strings are long, so never comma-joined."""
    return "\n".join(f"  - {item}" for item in items)


def _explode_problem_message(problems) -> str:
    """Offenders are connection strings registered in no environment file."""
    return (
        f"cimkit-git: {len(problems)} connection string(s) are registered in no "
        f"environment file:\n{_bullet_list(problems)}\n"
        f"add them to a connections/*.json (or run `git cim connections init`)"
    )


def _pack_problem_message(env, connections_file):
    """Offenders are referenced token keys missing from the chosen environment. The
    message names *which* file to fix — the old pack flow printed it and CI needs it to
    point the user at the right connections file."""
    if connections_file:
        where = str(connections_file)
    elif env:
        where = f"{conn.CONNECTIONS_DIR}/{env}.json"
    else:
        where = conn.LOCAL_FILE

    def describe(problems) -> str:
        return (
            f"cimkit-git: {where} is missing {len(problems)} referenced token "
            f"key(s):\n{_bullet_list(problems)}\nadd them to {where}"
        )

    return describe


class Substitution:
    """A two-phase environment-mode transform over parsed JSON entries.

    Construct via :meth:`for_explode` or :meth:`for_pack`; never directly. ``apply`` is
    called once per entry and accumulates problems; ``raise_if_problems`` fires once at
    the end.
    """

    def __init__(self, *, operation, mapping, fields, token, describe_problems):
        self._operation = operation       # conn.tokenize | conn.substitute
        self._mapping = mapping           # value->key (explode) or key->value (pack)
        self._fields = fields
        self._token = token
        # The factory owns its own wording — each already knows its direction, so the
        # message builder is injected rather than re-derived from a stored discriminant.
        self._describe_problems = describe_problems
        self._problems: "set[str]" = set()

    # ----------------------------------------------------------------- #
    # Factories — each sources its map from the ProjectConfig (issue 0002).
    # ----------------------------------------------------------------- #

    @classmethod
    def for_explode(cls, project_config) -> "Substitution":
        """Tokenize: replace real connection strings with ``@@key@@`` tokens, producing
        neutral source. Problems are connection strings registered in no **committed**
        environment file.

        The map comes from ``committed_reverse_map`` (``connections/*.json`` only, never
        ``local.json``): committed source must reference only keys that committed
        environments define, so a value living solely in a developer's ``local.json``
        is treated as unregistered and aborts explode rather than tokenising silently."""
        return cls(
            operation=conn.tokenize,
            mapping=project_config.committed_reverse_map(),
            fields=project_config.fields,
            token=project_config.token,
            describe_problems=_explode_problem_message,
        )

    @classmethod
    def for_pack(cls, project_config, env=None, connections_file=None) -> "Substitution":
        """Substitute: replace ``@@key@@`` tokens with one chosen environment's real
        connection strings. Precedence ``connections_file`` > ``env`` > ``local.json``
        (resolved by ``ProjectConfig.forward_map``). Problems are referenced token keys
        absent from the chosen environment."""
        return cls(
            operation=conn.substitute,
            mapping=project_config.forward_map(env, connections_file),
            fields=project_config.fields,
            token=project_config.token,
            describe_problems=_pack_problem_message(env, connections_file),
        )

    # ----------------------------------------------------------------- #
    # Phase 1: apply per entry, accumulating problems.
    # ----------------------------------------------------------------- #

    def apply(self, parsed):
        """Transform one parsed JSON entry in place. Any value that cannot be resolved is
        remembered (not raised) so the core can finish computing every entry first."""
        _, problems = self._operation(
            parsed, self._mapping, self._fields, self._token
        )
        self._problems |= problems

    # ----------------------------------------------------------------- #
    # Phase 2: fail once, listing every accumulated problem.
    # ----------------------------------------------------------------- #

    def raise_if_problems(self):
        """Raise :class:`SubstitutionError` if any ``apply`` recorded a problem; a no-op
        otherwise. Called after every entry is computed and before any output is written."""
        if self._problems:
            # Sort for determinism — the message feeds commit-blocking CI and must read
            # and reproduce the same every run, regardless of set iteration order.
            raise SubstitutionError(self._describe_problems(sorted(self._problems)))
