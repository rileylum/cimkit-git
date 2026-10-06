"""Regression tests for `build_working_copies` after the pack transform-seam cutover
(issue 0005).

`build_working_copies` backs the never-blocking post-merge/checkout/stash hooks (and
`git cim build`). When pack stopped taking `env=` and became connection-ignorant, the
rebuild had to construct the transform itself. These tests pin the safety properties of
that adaptation:

  * an environment-mode working copy is rebuilt with real connection strings (no token
    leak);
  * a project that *looks* connection-managed but never declared a mode is skipped, not
    silently packed with raw `@@tokens@@`;
  * a config that can't resolve (no connections) or is missing a key downgrades to a
    skip — a hook documented never to block must not crash.

Full per-project mode reading is issue 0009; this only locks down the cutover's edges.
"""

import json
import zipfile

from cimkit_git.hooks import build_working_copies
from cimkit_git.util import aprx_output_for


def _working_blob(src_dir):
    out = aprx_output_for(src_dir)
    with zipfile.ZipFile(out) as zf:
        return "".join(zf.read(n).decode("utf-8") for n in zf.namelist()
                       if n.endswith(".json"))


def test_env_working_copy_is_substituted(env_project, explode_env):
    # The working .aprx must carry the *chosen* environment's real value, never a token.
    # Build for `uat` (a value distinct from the fixture binary's) so this proves the
    # rebuild substituted rather than just leaving the original binary in place.
    src = explode_env(env_project.aprx)
    build_working_copies(src_dir=str(src), env="uat")
    blob = _working_blob(src)
    assert env_project.uat_value in blob
    assert "@@main@@" not in blob


def test_unmigrated_connection_project_is_skipped_not_leaked(env_project, explode_env):
    # connections/ + local.json present but aprx.json removed → no declared mode. The
    # rebuild must refuse, not pack with IDENTITY and write raw tokens into the binary —
    # so the pre-existing working .aprx is left byte-for-byte untouched.
    src = explode_env(env_project.aprx)
    (env_project.dir / "aprx.json").unlink()
    before = aprx_output_for(src).read_bytes()
    build_working_copies(src_dir=str(src))
    assert aprx_output_for(src).read_bytes() == before    # not rebuilt → nothing leaked


def test_missing_key_does_not_crash_the_hook(env_project, explode_env, capsys):
    # An env that resolves but is missing a referenced key raises SubstitutionError
    # inside pack; the never-blocking hook must swallow it as a skip, not a traceback.
    src = explode_env(env_project.aprx)
    # local.json is the default resolution; empty it so `main` is unresolved.
    (env_project.dir / "local.json").write_text(json.dumps({}))
    build_working_copies(src_dir=str(src))          # must not raise
    assert "skipping" in capsys.readouterr().err


def test_env_mode_without_connections_does_not_crash(env_project, explode_env, capsys):
    # aprx.json says env, but every connection source is gone → pack_transform sys.exits;
    # the hook downgrades that to a skip rather than aborting the whole rebuild.
    src = explode_env(env_project.aprx)
    (env_project.dir / "local.json").unlink()
    for name in ("dev.json", "uat.json"):
        (env_project.dir / "connections" / name).unlink()
    build_working_copies(src_dir=str(src))          # must not raise
    assert "skipping" in capsys.readouterr().err


def test_unreadable_connections_does_not_crash_the_hook(env_project, explode_env, deny_reading, capsys):
    # Issue 0009: a present-but-unreadable connections file (here local.json, the default
    # resolution) makes load_connections raise OSError. load_connections now converts that
    # to a SystemExit the hook already catches, so the never-blocking rebuild skips rather
    # than crashing — the same posture as its existing missing-key/no-connections skips.
    # deny_reading is applied *after* explode_env so setup's own reads succeed.
    src = explode_env(env_project.aprx)
    deny_reading(env_project.dir / "local.json")
    build_working_copies(src_dir=str(src))          # must not raise
    assert "skipping" in capsys.readouterr().err


def test_non_utf8_connections_does_not_crash_the_hook(env_project, explode_env, capsys):
    # Issue 0009: a non-UTF-8 connections file raises UnicodeDecodeError (a ValueError, not
    # an OSError) — the case the pre-0009 catch missed entirely. load_connections converts
    # it to a SystemExit, so the rebuild skips instead of crashing the post-* hook.
    src = explode_env(env_project.aprx)
    (env_project.dir / "local.json").write_bytes(b"\xff\xfe{ not utf-8 ")
    build_working_copies(src_dir=str(src))          # must not raise
    assert "skipping" in capsys.readouterr().err
