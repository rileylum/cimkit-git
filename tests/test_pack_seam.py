"""Tests for the pack transform seam (issue 0005).

pack is now the connection-ignorant version-control core (ADR-0002): it applies an
injected transform to each parsed JSON entry, then calls ``raise_if_problems`` once —
after every entry is computed but *before* the archive is opened. Simple mode injects
the no-op ``IDENTITY`` (a faithful, byte-stable rebuild); environment mode injects
``Substitution.for_pack`` (one environment's real connection strings).

These tests drive the contract **through the `pack` call** (the seam the suite already
uses for round-trips) and, for the flag-rejection rules that exist nowhere lower,
through **`main()`** — the thin CLI seam where ``--env`` / ``--connections`` are parsed.
"""

import json
import shutil
import zipfile

import pytest

from cimkit_git.pack import pack
from cimkit_git.project_config import ProjectConfig
from cimkit_git.transform import IDENTITY, Substitution, SubstitutionError


# --------------------------------------------------------------------------- #
# Simple mode — IDENTITY rebuild is faithful and deterministic (acceptance #2)
# --------------------------------------------------------------------------- #

def test_simple_pack_rebuilds_faithful_project(tmp_path, exploded, simple_aprx):
    # The default IDENTITY pack rebuilds a Project semantically identical to the source
    # it came from — same entries, nothing swapped — so adopting the seam changes no
    # simple-mode output. (`compare` returns False when the two match.)
    from cimkit_git.compare import compare
    out = pack(str(exploded), str(tmp_path / "out.aprx"))
    assert compare(str(simple_aprx), str(out)) is False


def test_pack_is_byte_identical_across_runs(tmp_path, exploded):
    # Closes the documented determinism gap: same Source packed twice → identical bytes
    # (fixed DOS-epoch timestamps + sorted entries + pinned compresslevel).
    first = pack(str(exploded), str(tmp_path / "a.aprx")).read_bytes()
    second = pack(str(exploded), str(tmp_path / "b.aprx")).read_bytes()
    assert first == second


# --------------------------------------------------------------------------- #
# Environment mode — substitute the chosen environment (acceptance #3)
# --------------------------------------------------------------------------- #

def _packed_layer(out):
    with zipfile.ZipFile(out) as zf:
        return zf.read("map/test_points.json").decode("utf-8")


def test_env_pack_substitutes_chosen_environment(env_project, explode_env, pack_env):
    src = explode_env(env_project.aprx)
    out = pack_env(src, output=env_project.dir / "uat.aprx", env="uat")
    data = _packed_layer(out)
    assert env_project.uat_value in data        # the chosen env's real value
    assert env_project.value not in data        # not dev/local's value
    assert "@@main@@" not in data               # no token left behind


def test_env_pack_precedence_connections_over_env(env_project, explode_env, pack_env):
    # --connections wins over --env: the explicit file's value is what lands.
    explicit = env_project.dir / "explicit.json"
    explicit.write_text(json.dumps({"main": "EXPLICIT-CONN"}))
    src = explode_env(env_project.aprx)
    out = pack_env(src, output=env_project.dir / "x.aprx",
                   env="uat", connections_file=str(explicit))
    data = _packed_layer(out)
    assert "EXPLICIT-CONN" in data
    assert env_project.uat_value not in data


# --------------------------------------------------------------------------- #
# Abort before writing — the compute-then-write guarantee (acceptance #4)
# --------------------------------------------------------------------------- #

def test_env_pack_missing_key_aborts_before_writing(env_project, explode_env):
    src = explode_env(env_project.aprx)
    # An environment that defines none of the referenced keys.
    (env_project.dir / "connections" / "broken.json").write_text("{}")
    out = env_project.dir / "broken.aprx"
    sub = Substitution.for_pack(ProjectConfig.load(env_project.dir), env="broken")

    with pytest.raises(SubstitutionError) as exc:
        pack(str(src), str(out), transform=sub)

    assert "main" in str(exc.value)   # the missing key is named
    assert not out.exists()           # nothing written


# --------------------------------------------------------------------------- #
# No resolvable connection values → error, never a token-filled Project
# (acceptance #5) — enforced when the transform is built
# --------------------------------------------------------------------------- #

def test_env_pack_with_nothing_resolvable_errors(env_project, explode_env, pack_env):
    src = explode_env(env_project.aprx)
    (env_project.dir / "local.json").unlink()   # no flag, no local.json → nothing resolves
    with pytest.raises(SystemExit) as exc:
        pack_env(src, output=env_project.dir / "x.aprx")   # no env, no connections
    assert "no connection values" in str(exc.value)


# --------------------------------------------------------------------------- #
# Simple-mode flag rejection — only exists at the CLI seam (acceptance #6)
# --------------------------------------------------------------------------- #

def _simple_project(tmp_path, simple_aprx):
    """A simple-mode project: map.aprx + aprx.json(mode: simple) + its exploded src,
    all in one directory so pack's composition root finds the aprx.json beside the src."""
    from cimkit_git.explode import explode
    proj = tmp_path / "proj"
    proj.mkdir()
    shutil.copy(simple_aprx, proj / "map.aprx")
    (proj / "aprx.json").write_text(json.dumps({"mode": "simple"}))
    return explode(str(proj / "map.aprx"), str(proj / "map.aprx.src"))


def _run_main(monkeypatch, argv):
    from cimkit_git.__main__ import main
    monkeypatch.setattr("sys.argv", ["aprx", *argv])
    return main


def test_main_rejects_env_flag_on_simple_project(monkeypatch, tmp_path, simple_aprx):
    # A simple-mode project declares mode: simple; --env must be refused, not silently
    # ignored — the master-switch rule (ADR-0001) surfaced at the CLI seam.
    src = _simple_project(tmp_path, simple_aprx)
    main = _run_main(monkeypatch, ["pack", str(src), "--env", "uat"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert "not an environment-mode project" in str(exc.value)


def test_main_rejects_connections_flag_on_simple_project(monkeypatch, tmp_path, simple_aprx):
    src = _simple_project(tmp_path, simple_aprx)
    main = _run_main(monkeypatch, ["pack", str(src), "--connections", "x.json"])
    with pytest.raises(SystemExit) as exc:
        main()
    assert "not an environment-mode project" in str(exc.value)


# --------------------------------------------------------------------------- #
# pack no longer knows about connections (acceptance #1)
# --------------------------------------------------------------------------- #

def test_pack_module_does_not_import_connection_engine():
    import cimkit_git.pack as pack_mod
    assert not hasattr(pack_mod, "conn")
    assert pack_mod.pack.__defaults__[-1] is IDENTITY   # default transform is the no-op
