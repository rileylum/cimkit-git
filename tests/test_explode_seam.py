"""Tests for the explode transform seam (issue 0004).

explode is now the connection-ignorant version-control core (ADR-0002): it applies an
injected transform to each parsed JSON entry, then calls ``raise_if_problems`` once —
after every entry is computed but *before* anything is written. Simple mode injects the
no-op ``IDENTITY`` (a faithful render); environment mode injects
``Substitution.for_explode`` (neutral source).

These tests drive the contract **through the `explode` call** — the seam the suite
already uses for round-trips — never through private helpers. The abort-before-write and
committed-only cases are the security-critical ones: a regression there leaks a raw
connection string into committed source.
"""

import json
import shutil

import pytest

from cimkit_git.explode import explode
from cimkit_git.project_config import ProjectConfig
from cimkit_git.transform import Substitution, SubstitutionError


# --------------------------------------------------------------------------- #
# Simple mode — the migrated fixture resolves to IDENTITY
# (the bare-explode faithful render itself is covered by
# test_connections.py::test_simple_mode_untouched)
# --------------------------------------------------------------------------- #

def test_simple_fixture_declares_simple_mode(simple_aprx):
    # Acceptance #6: the migrated fixture carries an aprx.json the composition root
    # reads to pick IDENTITY over Substitution.
    cfg = ProjectConfig.load(simple_aprx.parent)
    assert cfg.mode == "simple"
    assert cfg.is_env is False


# --------------------------------------------------------------------------- #
# Environment mode — explode always produces neutral source
# --------------------------------------------------------------------------- #

def test_env_explode_produces_neutral_source(env_project):
    # Even run directly (outside the hooks), every configured connection string becomes
    # a token — no raw connection string can reach committed source.
    cfg = ProjectConfig.load(env_project.dir)
    out = explode(str(env_project.aprx), transform=Substitution.for_explode(cfg))
    blob = "".join(p.read_text() for p in (out / "map").rglob("*.json"))
    assert env_project.value not in blob
    assert "@@main@@" in blob


# --------------------------------------------------------------------------- #
# Abort before writing — the compute-then-write guarantee
# --------------------------------------------------------------------------- #

def test_env_explode_aborts_before_writing_on_unknown(env_project):
    # Point the committed envs away from the fixture's real value so it is unregistered.
    for name in ("dev.json", "uat.json"):
        (env_project.dir / "connections" / name).write_text(json.dumps({"main": "OTHER"}))
    cfg = ProjectConfig.load(env_project.dir)
    out = env_project.dir / "map.aprx.src"

    with pytest.raises(SubstitutionError) as exc:
        explode(str(env_project.aprx), str(out), transform=Substitution.for_explode(cfg))

    assert env_project.value in str(exc.value)   # the offender is listed
    assert not out.exists()                       # nothing half-written


def test_env_explode_unknown_leaves_existing_source_intact(env_project):
    # The strongest form of the guarantee: a re-explode that fails must not clobber the
    # previously-good src directory.
    for name in ("dev.json", "uat.json"):
        (env_project.dir / "connections" / name).write_text(json.dumps({"main": "OTHER"}))
    cfg = ProjectConfig.load(env_project.dir)
    out = env_project.dir / "map.aprx.src"
    out.mkdir()
    (out / "keep.txt").write_text("previous good source")

    with pytest.raises(SubstitutionError):
        explode(str(env_project.aprx), str(out), transform=Substitution.for_explode(cfg))

    assert (out / "keep.txt").read_text() == "previous good source"


# --------------------------------------------------------------------------- #
# Committed-only tokenisation (acceptance #5)
# --------------------------------------------------------------------------- #

def test_local_only_value_is_unregistered(env_project):
    # The fixture's real value lives ONLY in local.json; the committed envs point
    # elsewhere. explode tokenises against committed files only, so the value is
    # unregistered and aborts — it never silently tokenises into committed source.
    for name in ("dev.json", "uat.json"):
        (env_project.dir / "connections" / name).write_text(json.dumps({"main": "OTHER"}))
    (env_project.dir / "local.json").write_text(json.dumps({"main": env_project.value}))
    cfg = ProjectConfig.load(env_project.dir)
    out = env_project.dir / "map.aprx.src"

    with pytest.raises(SubstitutionError) as exc:
        explode(str(env_project.aprx), str(out), transform=Substitution.for_explode(cfg))

    assert env_project.value in str(exc.value)
    assert not out.exists()


# --------------------------------------------------------------------------- #
# Environment mode with no committed connection files → one clear abort
# --------------------------------------------------------------------------- #

def test_env_mode_without_committed_connections_aborts_clearly(tmp_path, simple_aprx):
    # No connections/*.json at all: rather than reporting every real connection string
    # as 'unregistered' one-by-one, the composition fails fast with a single message.
    proj = tmp_path / "p"
    proj.mkdir()
    aprx = proj / "map.aprx"
    shutil.copy(simple_aprx, aprx)
    (proj / "aprx.json").write_text(json.dumps({"mode": "env"}))
    cfg = ProjectConfig.load(proj)

    with pytest.raises(SystemExit) as exc:
        Substitution.for_explode(cfg)   # builds the committed reverse map eagerly

    assert "connections" in str(exc.value)
