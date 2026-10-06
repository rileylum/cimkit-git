"""Tests for the Substitution transform adapter (issue 0003).

Substitution is the environment-mode adapter at the explode/pack transform seam
(ADR-0002). It is two-phase: ``apply(parsed)`` mutates one parsed JSON entry in place
and accumulates problems across calls; ``raise_if_problems()`` then fails once, listing
every offender, *before* the core writes anything.

These tests drive Substitution directly, built from a ``ProjectConfig`` fixture — never
through explode/pack (that wiring lands in 0004/0005). They assert external behaviour:
what the parsed entry looks like after ``apply``, and that ``raise_if_problems`` surfaces
all offenders — not private helper shapes.
"""

import json

import pytest

from cimkit_git import connections as conn
from cimkit_git.project_config import ProjectConfig
from cimkit_git.transform import Substitution, SubstitutionError


# --------------------------------------------------------------------------- #
# Fixtures: a ProjectConfig built from a real aprx.json + connections/
# --------------------------------------------------------------------------- #

def _env_config(tmp_path, dev=None, uat=None, local=None, **cfg):
    """An env-mode ProjectConfig backed by aprx.json + the given connection files."""
    (tmp_path / conn.CONFIG_FILENAME).write_text(
        json.dumps({"mode": "env", **cfg}), encoding="utf-8"
    )
    (tmp_path / conn.CONNECTIONS_DIR).mkdir()
    if dev is not None:
        (tmp_path / conn.CONNECTIONS_DIR / "dev.json").write_text(json.dumps(dev))
    if uat is not None:
        (tmp_path / conn.CONNECTIONS_DIR / "uat.json").write_text(json.dumps(uat))
    if local is not None:
        (tmp_path / conn.LOCAL_FILE).write_text(json.dumps(local))
    return ProjectConfig.load(tmp_path)


def _entry(value):
    """A parsed JSON entry carrying *value* in a configured field, nested for realism."""
    return {"layer": {"dataConnection": {"workspaceConnectionString": value, "dataset": "x"}}}


def _field_value(entry):
    return entry["layer"]["dataConnection"]["workspaceConnectionString"]


# --------------------------------------------------------------------------- #
# for_explode — tokenize (value -> token), problems are unregistered values
# --------------------------------------------------------------------------- #

def test_for_explode_tokenizes_known_value(tmp_path):
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"}, uat={"main": "UAT-CONN"})
    sub = Substitution.for_explode(cfg)

    entry = _entry("DEV-CONN")
    sub.apply(entry)

    assert _field_value(entry) == "@@main@@"
    sub.raise_if_problems()  # clean — no-op


def test_for_explode_accumulates_every_unregistered_value(tmp_path):
    # Two entries, two *different* unregistered values: raise_if_problems must list both,
    # not just the first — the user fixes them all at once and never half-writes source.
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"})
    sub = Substitution.for_explode(cfg)

    sub.apply(_entry("UNKNOWN-A"))
    sub.apply(_entry("UNKNOWN-B"))

    with pytest.raises(SubstitutionError) as exc:
        sub.raise_if_problems()
    message = str(exc.value)
    assert "UNKNOWN-A" in message
    assert "UNKNOWN-B" in message


def test_for_explode_leaves_already_tokenised_value_untouched(tmp_path):
    # Idempotence: explode running over already-neutral source is a no-op, not an error.
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"})
    sub = Substitution.for_explode(cfg)

    entry = _entry("@@main@@")
    sub.apply(entry)

    assert _field_value(entry) == "@@main@@"
    sub.raise_if_problems()  # no problems


# --------------------------------------------------------------------------- #
# for_pack — substitute (token -> value), problems are missing keys
# --------------------------------------------------------------------------- #

def test_for_pack_substitutes_token_for_chosen_environment(tmp_path):
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"}, uat={"main": "UAT-CONN"})
    sub = Substitution.for_pack(cfg, env="uat")

    entry = _entry("@@main@@")
    sub.apply(entry)

    assert _field_value(entry) == "UAT-CONN"
    sub.raise_if_problems()


def test_for_pack_precedence_connections_file_over_env(tmp_path):
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"}, uat={"main": "UAT-CONN"})
    explicit = tmp_path / "explicit.json"
    explicit.write_text(json.dumps({"main": "EXPLICIT-CONN"}))
    sub = Substitution.for_pack(cfg, env="uat", connections_file=str(explicit))

    entry = _entry("@@main@@")
    sub.apply(entry)

    assert _field_value(entry) == "EXPLICIT-CONN"


def test_for_pack_accumulates_every_missing_key(tmp_path):
    # The chosen environment defines `main` but not the keys referenced here.
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"})
    sub = Substitution.for_pack(cfg, env="dev")

    sub.apply(_entry("@@absent_one@@"))
    sub.apply(_entry("@@absent_two@@"))

    with pytest.raises(SubstitutionError) as exc:
        sub.raise_if_problems()
    message = str(exc.value)
    assert "absent_one" in message
    assert "absent_two" in message


def test_for_pack_error_names_the_offending_environment_file(tmp_path):
    # The message must point at *which* connections file to fix, not just say
    # "the chosen environment" — CI output needs to identify the file.
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"}, uat={"other": "UAT-CONN"})
    sub = Substitution.for_pack(cfg, env="uat")
    sub.apply(_entry("@@main@@"))

    with pytest.raises(SubstitutionError) as exc:
        sub.raise_if_problems()
    assert "uat.json" in str(exc.value)


# --------------------------------------------------------------------------- #
# Clean entry / no-op
# --------------------------------------------------------------------------- #

def test_apply_clean_entry_records_no_problems(tmp_path):
    cfg = _env_config(tmp_path, dev={"main": "DEV-CONN"})
    sub = Substitution.for_pack(cfg, env="dev")

    # An entry with no configured field at all: nothing to do, no problems.
    sub.apply({"metadata": {"title": "no connections here"}})
    sub.raise_if_problems()  # no-op


# --------------------------------------------------------------------------- #
# Special characters round-trip — operates on parsed JSON, never text
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize(
    "value",
    [
        r"SERVER=db\instance;DATABASE=acme",            # backslash + semicolons
        r'AUTHENTICATION_MODE="OSA";USER=gis',          # quotes + semicolon
        r"DATABASE=.\local.gdb;FLAGS=a;b;c",            # leading dot-backslash, many ;
    ],
)
def test_special_characters_round_trip(tmp_path, value):
    # explode (value -> token) then pack (token -> value) must reproduce the byte-for-byte
    # original. Tokenising parsed JSON rather than raw text is what preserves the escaping.
    cfg = _env_config(tmp_path, dev={"main": value})

    exploded = _entry(value)
    Substitution.for_explode(cfg).apply(exploded)
    assert _field_value(exploded) == "@@main@@"

    packed = _entry("@@main@@")
    Substitution.for_pack(cfg, env="dev").apply(packed)
    assert _field_value(packed) == value


# --------------------------------------------------------------------------- #
# Custom token format / fields flow through from ProjectConfig
# --------------------------------------------------------------------------- #

def test_respects_custom_token_and_fields_from_config(tmp_path):
    cfg = _env_config(
        tmp_path, dev={"main": "DEV-CONN"}, token="<<{key}>>", fields=["wcs"],
    )
    sub = Substitution.for_explode(cfg)

    entry = {"wcs": "DEV-CONN", "workspaceConnectionString": "DEV-CONN"}
    sub.apply(entry)

    # only the configured field (`wcs`) is tokenised, with the configured token format
    assert entry["wcs"] == "<<main>>"
    assert entry["workspaceConnectionString"] == "DEV-CONN"
