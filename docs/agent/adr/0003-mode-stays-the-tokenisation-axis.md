# Mode stays the tokenisation axis — not a per-command strategy object

## Status

accepted

## Context & decision

An architecture review (2026-06-25, candidate D) proposed collapsing the simple/env
distinction into a polymorphic `cfg.mode` — two adapters `SimpleMode` / `EnvMode`
exposing methods like `transform_for(...)`, `verify(source)`, `buildable(...)` — to
replace the ~5 sites that branch on a bare `if cfg.is_env`:

- `transform.py` explode/pack — `IDENTITY` vs `Substitution`
- `verify.py` — `_verify_simple_project` vs `_verify_env_project`
- `hooks.py` `build_working_copies` — env + no connections file → skip
- `hooks.py` `hook_pre_commit` — env unstages the binary, simple explodes + stages source

**Rejected.** Mode is *one* axis: **does Source get tokenised** (simple = faithful,
environment-independent render; env = neutral, tokenised render — see CONTEXT.md "Mode").
The behaviours D wanted to absorb are not parameterisations of that one axis; they are
either already deep at the transform seam, or *consequences* of the axis that should be
derived, not bundled. Folding them under a `Mode` god-object would redefine Mode from a
crisp single distinction into a grab-bag of per-command behaviours — the exact
"connection logic spread thin / core made shallower" outcome ADR-0002 rejected. The
`if cfg.is_env` branches stay explicit, sited where the mode is read.

## Why the branch count is misleading

- **The transform pair is already the deep version.** `IDENTITY` and `Substitution`
  implement the *same* `apply()` / `raise_if_problems()` contract, applied identically by
  the connection-ignorant core (ADR-0002). That *is* mode-as-strategy, already built, for
  the one operation where the two implementations share a real interface.
- **`verify`'s two bodies share no interface — only a signature.** `_verify_env_project`
  (scan Source for raw strings; check every referenced key resolves in every committed
  env file) and `_verify_simple_project` (pack Source into a temp dir; byte-compare to the
  committed binary) do unrelated work. A `mode.verify()` method would route to two
  unrelated bodies — the fork relocated into a vtable, not removed, and arguably
  *shallower* than a branch sited where the mode is read.
- **`build` is mostly handled by the transform seam already.** `pack_transform` carries
  the mode difference; the only residual branch is a one-line guard (env + no connections
  file → skip).

## The load-bearing insight (binary lifecycle is downstream, not part, of Mode)

The review framed "is the binary committed?" as a second behaviour to put on the Mode
object. Working it through showed it is **not a property of Mode at all** — it is
*constrained by* the tokenisation axis, and otherwise an author-set policy:

- Tokenisation decides whether a committable binary even *exists*. In env mode the only
  binaries that exist are resolved per-environment artifacts (a tokenised `.aprx` full of
  `@@token@@` won't open in Pro), so there is nothing environment-neutral to commit.
- In simple mode a single faithful, neutral binary exists, so whether to commit it is the
  author's choice (see issue 0006).

So binary lifecycle (commit vs gitignore, which env on pull, what a commit emits) is a
separate concern that *correlates with* Mode without *being* Mode. Putting it on a
`SimpleMode`/`EnvMode` object would weld together axes that must stay separable — the
opposite of a deepening.

## Considered options

- **One `Mode` strategy object with N methods (candidate D)** — rejected, as above:
  relocates forks that share no interface, redefines Mode, and reopens ADR-0002's scope.
- **Keep explicit branches, sited at the mode read** — accepted: each branch lives where
  the mode is loaded, the one genuinely-polymorphic operation (the transform) already has
  its seam, and unrelated operations stay legibly unrelated.

## Consequences

- ADR-0002's scope (the Mode seam is explode/pack only) stands; this ADR records *why* so
  future reviews stop re-proposing a per-command Mode strategy.
- A real, separable issue surfaced and is tracked independently: simple-mode `verify`
  *requires* the committed binary, when by the "Source is the committed truth, the binary
  is optional" principle it should check sync only when a binary is present, and binary
  lifecycle should be author-configurable at `aprx install`. See issue 0006.
