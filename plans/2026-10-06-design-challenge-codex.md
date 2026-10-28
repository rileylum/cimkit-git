<!-- model: gpt-6-astra, reasoning effort xhigh; reviewed DESIGN.md sha256 75a289ee… against main @ b25c49e -->
# Adversarial review — DESIGN.md redesign

Reviewed: `b25c49e62d1d9e6a3e72e2b985033fa99d2635fb` (supporting code) · Base: `main @ b25c49e`
Verdict: needs-attention

The proposed sync and validation contracts still permit lost edits, incorrect database bindings, and environment values reaching Git. Resolve these contracts before implementing automatic writes.

The review target is the untracked `DESIGN.md`, not a code diff: HEAD, main, and their merge base are identical. Design SHA-256: `75a289eeae2c6bf05a5543642b0ff78c3fef475a8a4ed0ab7cd55808501aa055`. Existing code and tests below are evidence of concrete operations and failure mechanisms, not requirements inherited from the deleted ADRs.

## Findings

### [high] A target name cannot recover the values used to build a binary — `DESIGN.md:71-89`

Confidence: 0.99

Build a binary from `@@main@@` with target `dev` mapping `main → DATABASE=A` and `archive → DATABASE=B`. Then change that target's local file or environment so those values swap. Neither recorded file hash changes, so the table reports clean and leaves the binary pointing at the old database. After a GUI edit, explode uses the current `dev` values and replaces `DATABASE=A` with `@@archive@@`. Every value still maps to exactly one key, so the stated ambiguity guard passes and the wrong logical database binding is committed. A one-off `--value` disappearing before the next hook is another instance of losing the original mapping.

This follows from recording only two file hashes and a target name while allowing mutable layers (`DESIGN.md:140-156`). The current substitution functions perform exactly this reversal (`cimkit_git/connections.py:175-215`); a read-only in-memory example returned `@@archive@@` with no problems after the swap.

Record recoverable provenance for the effective build mapping and placeholder rules, not just the target label. Reverse against that immutable mapping; separately detect changed desired values/rules as a stale build. If the original mapping cannot be recovered, require explicit adoption or recovery without overwriting either side. This must cover CLI overrides and changed process environments.

### [high] Auto-staging can overwrite an independent edit held only in the index — `DESIGN.md:77-86`

Confidence: 0.98

Start with committed, synchronized Source `S0`. Edit Source to `S1` and stage it, then restore only its working files from HEAD. The index still holds `S1`, but working Source is back to `S0`. Now edit the binary. The proposed table sees only “binary changed, Source same”; pre-commit explodes and stages the generated Source (`DESIGN.md:111-112`), replacing `S1` without reporting a conflict. The staged edit can disappear from the intended commit. Restoring only the worktree while preserving the index is an ordinary supported [Git operation](https://git-scm.com/docs/git-restore).

The index is absent from both the record and decision table. The current planner explicitly consults staged Source paths, including deletions, before deciding whether to refresh from a binary (`cimkit_git/hooks.py:210-237`); the generated Source is then staged wholesale (`cimkit_git/hooks.py:304-308`). Existing staged-edit/deletion cases are covered in `tests/test_hooks_pre_commit.py:152-225`, although those tests do not exercise this index/worktree divergence.

Give pre-commit a decision contract that includes the actual index content independently of working Source. Refuse automatic replacement when staged content contains an independent change; preserve partial staging and staged deletions. The staged leak check must also use configuration from the same index snapshot. Sharing transformation code with commands is safe; treating the command's two-file state as sufficient authority to rewrite the index is not.

### [high] Raw-byte fallback leaves the neutrality gate unable to inspect malformed Source — `DESIGN.md:60-63`

Confidence: 0.97

A Source JSON entry containing a real `workspaceConnectionString` plus a trailing comma or merge-conflict marker cannot be traversed by a field-based scanner. The design explicitly preserves anything unparseable as raw bytes, but does not require the neutrality/buildability gates to fail when those bytes cannot be inspected. Carrying forward the current policy would allow the raw connection through explode, staging, and `check`; it can also emit an unresolved or malformed entry into a built binary.

This is a demonstrated current failure mechanism: JSON parse errors are stored on entries (`cimkit_git/entry.py:101-114`), rendering returns their original bytes (`cimkit_git/entry.py:210-227`), and verification discards them before checking for leaks (`cimkit_git/entry.py:153-167`, `cimkit_git/verify.py:38-48`). `tests/test_entry.py:109-122` explicitly expects passthrough and scan omission. A read-only example containing a synthetic raw connection string confirmed both behaviors. The redesign does not yet specify a different validation policy.

Distinguish known opaque payloads from structured entries that failed parsing. Preserve bytes for recovery if needed, but make malformed inspectable entries a blocking problem for automatic staging, `check`, and normal build. Define which entry types each codec can certify for neutrality; an uninspectable entry must not be treated as evidence of a clean scan.

### [high] A working-tree check does not validate what pre-push publishes — `DESIGN.md:111-115`

Confidence: 0.96

Commit a raw connection string while hooks are unavailable, then neutralise it only in the working tree. A pre-push implementation that merely runs the ordinary `check` sees neutral Source and permits the leaking commit to be uploaded. Checking HEAD instead is still insufficient when pushing a different local branch, or when a later cleanup commit removes a secret that remains in an earlier outgoing commit. Those are normal push shapes, not concurrent-write edge cases.

This is the existing call path: `hook_pre_push()` calls `verify()` without a revision (`cimkit_git/hooks.py:512-517`), and `verify()` discovers and reads working directories (`cimkit_git/verify.py:117-142`). Its hook tests mutate working Source and call the hook directly (`tests/test_hooks_pre_commit.py:344-356`). The proposed replacement specifies no commit-tree input or outgoing-ref traversal. Git supplies the local and remote object IDs on [pre-push standard input](https://git-scm.com/docs/githooks#_pre_push), which is the relevant publication boundary.

Make validation operate on an explicit immutable tree and its corresponding config. Have pre-push consume every supplied ref update and validate outgoing Source, including newly published history for the no-leak guarantee. Define handling for new branches and deleted refs. Keep the working-tree form of `check` as a separate invocation, not the implicit push input.

### [high] A successful state comparison does not protect the subsequent write — `DESIGN.md:77-98`

Confidence: 0.94

After Source changes, a post-checkout build observes the binary matching its recorded hash and decides it may overwrite it. Pro then saves an edit before the build publishes its output. Replacing the binary now destroys that edit despite the earlier “unambiguous” state. The opposite race exists when Source is edited after an explode's comparison. Concurrent tool invocations can also publish files and state records from different operations. The pending Pro lock question (`DESIGN.md:192-193`) does not establish a write protocol.

The current write paths offer no protection to inherit: pack opens the destination with `"w"` (`cimkit_git/pack.py:52-56`), and explode removes the existing Source directory before writing its replacement (`cimkit_git/explode.py:44-54`). They validate transformation problems before writing, but provide neither exclusion nor rollback for a failed write. The new hashes improve detection before these operations; they do not make the operations atomic or stop another writer.

Specify a transaction covering input snapshots, output publication, and state advancement. Serialize tool operations, validate complete output before publishing it, retain the previous output on failure, and record hashes of the exact snapshots consumed and produced. Establish how a GUI writer is excluded during publication; if that cannot be guaranteed, refuse automatic overwrite while the project is open or build to a separate output. A one-time lock-file existence check or atomic rename alone does not prevent the lost-save interleaving.

### [high] Blanket fail-open exception handling defeats the blocking hooks — `DESIGN.md:55-56`

Confidence: 0.93

The architecture says library failures are typed exceptions and the hook entry point converts them into fail-open warnings. Applied as written, a staged leak, sync conflict, or unreadable config becomes a warning and a successful pre-commit/pre-push exit. Git can then publish the very content that lines 111-115 say must be blocked. Even if known conflict results are handled specially, failure to read the staged config or complete the scan must not be interpreted as successful validation.

There is a concrete distinction in the current implementation: hook installation marks pre-commit and pre-push as blocking, and only post hooks are non-blocking (`cimkit_git/install.py:44-70`). Pre-commit's blocked-project path aborts before writes (`cimkit_git/hooks.py:397-398`); pre-push propagates the verification failure. The draft's unqualified fail-open rule removes that distinction. This is an unresolved contract contradiction, not a claim that an unimplemented exception handler has been executed.

Specify error handling by hook and operation. Required pre-commit/pre-push validation must fail closed on a finding or an inability to validate; post-checkout/post-merge may warn and skip. Any best-effort refresh of an unrelated project must be isolated from the validation of content actually being committed.

### [high] The entry contract lacks the path boundary needed for safe extraction — `DESIGN.md:60-63`

Confidence: 0.91

An imported `.aprx` can contain an entry named `../cimkit.toml` or an absolute filename. Treating each `(name, bytes)` pair as a relative Source destination writes outside `map.aprx.src`, allowing a project archive to overwrite configuration or other user files. Sorted entry order and faithful byte rendering do not constrain destinations. Duplicate or platform-equivalent names can also collapse distinct entries into one file, losing data on the Windows platform this package serves.

The current reader yields archive names unchanged (`cimkit_git/entry.py:135-137`); explode joins each name onto the output directory and writes it without containment validation (`cimkit_git/explode.py:48-54`). The proposed codec/Source split retains that name-to-files interface without assigning validation to either side. The design risk is retaining this demonstrably unsafe behavior during the rewrite, not an assertion that the new codec already exists.

Require a validated entry namespace before any filesystem mutation: reject absolute paths, parent traversal, drive/UNC forms, duplicate destinations, and names that collide under supported filesystem rules. Prevent symlink escapes when reading or writing Source. Put these requirements at the codec/Source boundary so future format adapters cannot bypass them.

### [medium] Secret-only targets have no authoritative inventory for “check every target” — `DESIGN.md:149-159`

Confidence: 0.95

The design explicitly allows a team to commit no value files and provide values entirely through local files or CI environment variables. Nothing declares which targets must exist. If CI omits every `CIMKIT_PRD_*` variable, a checker that discovers targets from available values can check dev/uat and silently omit prd; a checker that requires committed target files instead rejects the supported secret-only setup. Local pre-push has the same ambiguity when the developer intentionally lacks production values.

Current verification gets its target inventory from `connections/*.json`, rejects an empty inventory, and iterates those files (`cimkit_git/verify.py:55-77`). Making that layer optional removes the inventory on which the existing all-target assertion depends. Neither the new config section nor the command table supplies a replacement. The risk is inferred from that missing contract; no new target-discovery implementation exists to execute.

Declare required target names independently of their values in committed configuration. Define explicit target selection for checks and CI, make absent required targets fail rather than disappear, and distinguish a neutrality-only local check from proof that every deployment target resolves. This lets developers commit without production credentials while CI still verifies the complete declared target set.

## Not verified

- No redesign code exists, so proposed sync decisions and interleavings were reviewed as contracts, not executed as regression tests. Supporting code and test cases were read; only read-only, in-memory transformation examples and fixture inspection were executed.
- The test suite was not run, as required by the adversarial-review skill. Existing tests cited here are evidence of intended scenarios, not a claim that the suite passes.
- ArcGIS Pro behavior, Windows locking/filesystem behavior, crash recovery, cross-machine byte determinism, and `cimkit-corpus` round trips were not exercised. In particular, there is no verified mechanism here that prevents Pro from saving during a build.
- Git's documented hook interfaces were checked, but installation and execution of the proposed hooks, the pre-commit framework integration, and CI secret provisioning were not exercised.
- The pre-existing deletions under `docs/agent/` were outside this design review and were left untouched. Only `CHALLENGE.md` was written.
