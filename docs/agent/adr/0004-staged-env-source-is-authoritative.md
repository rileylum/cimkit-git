# A staged env Source is authoritative — re-tokenise it in place, don't re-explode the binary

## Status

accepted

## Context & decision

Environment mode keeps the working `.aprx` as a gitignored build artifact and commits only
neutral (tokenised) Source. The pre-commit hook's env sweep originally re-exploded the
working binary into Source for **every** env Project on every commit, gated only on Mode.
That sweep does two jobs at once: it *re-derives* Source from the binary (so editing the
binary in Pro updates Source) **and** it *guarantees neutrality* (explode always tokenises).

Those two jobs collide on the merge-resolution path. A developer resolves a conflict by
hand-editing the tokenised `.aprx.src/` JSON and stages the **Source only** (never the
binary — the env workflow). The working `.aprx` is **stale** at that moment: a conflicted
`git merge` never fires `post-merge`, so `build_working_copies` never rebuilt the binary
from the merged Source. The unconditional sweep then re-exploded that stale binary over the
staged resolution and re-staged it — **silently destroying hand-resolved merge work**, the
most expensive kind of edit to lose (issue 0007). Reachability was confirmed, not
hypothetical: nothing between conflict-resolution and commit rebuilds the binary.

**Decision.** The env pre-commit path splits in two, chosen by whether the developer staged
Source inside that Project this commit:

- **No staged Source** → `refresh_env`: re-explode the working binary into neutral Source.
  The normal flow — the developer edited the binary, and Source is derived from it.
- **Staged Source** (a hand-resolved merge) → `retokenize_env`: keep the developer's Source
  and re-tokenise it **in place**, passing each parsed entry through the same value→token
  transform `explode` uses (`_retokenize_staged_source`). On already-neutral Source it is a
  no-op; a *registered* raw connection string left behind is replaced by its token.

A staged Source is treated as authoritative: the developer's explicit `git add` of Source
outweighs an implicit, possibly-stale binary edit. The split lives in the pure
`plan_precommit` seam (issue 0001), so the routing is assertable on the `StagePlan` value
with no throwaway repo; `retokenize_env` is the new field carrying it.

## Why in-place re-tokenise, not the simpler alternatives

The fix must separate the sweep's two jobs — stop *clobbering* (re-deriving from the binary)
while keeping *neutrality* (tokenisation). Three options were weighed (the issue's A/B/C):

- **Skip the refresh when Source is staged (Option A)** — rejected: it drops *both* jobs. A
  high-effort review confirmed it lets a raw connection string left in hand-staged Source
  reach the commit verbatim, caught only by the bypassable pre-push `verify`. That violates
  the leak guarantee the sweep used to provide as a side effect of clobbering.
- **Re-tokenise the staged Source in place (Option B)** — accepted: preserves the
  developer's content *and* re-asserts neutrality, separating the two jobs cleanly. It reuses
  the existing `entry.read_entries` / `render_pretty` seam (which reads a Source dir
  transparently) and `explode`'s two-phase *compute-then-write* contract, so an unresolvable
  value aborts the dir before it overwrites the staged Source.
- **Detect and hard-block (Option C)** — folded into B's fail-open posture rather than
  adopted wholesale: see below.

## The fail-open boundary (skip, not block — and where the real gate is)

`_retokenize_staged_source` **skips with a hint** (never aborts the whole commit) on an
unresolvable value — a raw string registered in no committed environment — or an unreadable
config, mirroring `_refresh_env_source`. This keeps the clean property that pre-commit blocks
for exactly one reason: an undeclared Mode, decided in the pure plan before any index change
(ADR-0001). The leak backstop for what re-tokenise can't neutralise on its own is the
pre-push / CI `aprx verify` gate — the same backstop the from-binary refresh already relied
on for an unregistered value in the binary. So neutrality is *enforced* at commit for every
value the tool knows (registered in `connections/*.json`) and *gated* at push for the rest;
no path makes pre-commit a second blocking gate.

## Consequences

- **The env binary is never committed on either path.** Pack-exclusion for env Projects is
  the staged-Source loop's job (its ENV branch), independent of which env list a dir lands
  in — so neither `refresh_env` nor `retokenize_env` can leak the binary into the commit.
- **The suppression signal counts deletions.** Staging the *removal* of a Source entry (a
  merge that drops a layer) routes the dir to `retokenize_env`, so the from-binary refresh no
  longer resurrects the deleted entry. This needs a deletion-inclusive staged set
  (`_staged_incl_deletions`) separate from the ACM `staged` the explode/pack/block decisions
  use — those only ever act on a file that still exists.
- **Accepted tradeoff.** The signal keys on *any* staged path inside a `.aprx.src/` dir. If a
  developer both edits the working binary and stages a file under that `.src/`, the binary
  edit is not re-exploded — explicit staging wins. Benign in practice (the `.src/` tree is
  generated; users don't hand-add files there) and the working binary is never lost on disk.

## References

- ADR-0001 (Mode is declared; env always tokenises; the binary is never committed) and
  ADR-0002 (the explode/pack transform seam `_retokenize_staged_source` reuses).
- Issue 0007 (this change) and issue 0004 (the Source-only merge-resolution path it shares).
- CONTEXT.md: Source, Neutral source, Environment mode, Token, Tokenize.
