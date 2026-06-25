"""Tests for the ProjectConfig.load seam (issue 0002).

ProjectConfig is the single home for the resolution ritual: it reads a Project's
declared mode/fields/token from the committed ``aprx.json`` and, for environment-mode
Projects, discovers the connection files and builds the token<->value maps. These tests
drive only that seam — they build a temporary ``aprx.json`` (plus ``connections/`` for
the env cases) and assert external behaviour, never private helper shapes.
"""

import json

import pytest

from aprx_tools import connections as conn
from aprx_tools.project_config import ProjectConfig


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def _write_config(project_dir, **fields):
    (project_dir / conn.CONFIG_FILENAME).write_text(
        json.dumps(fields), encoding="utf-8"
    )


# --------------------------------------------------------------------------- #
# Reading mode / fields / token
# --------------------------------------------------------------------------- #

def test_load_reads_simple_mode(tmp_path):
    _write_config(tmp_path, mode="simple")
    cfg = ProjectConfig.load(tmp_path)
    assert cfg.mode == "simple"
    assert cfg.is_env is False
    # defaults apply when fields/token are omitted
    assert tuple(cfg.fields) == conn.DEFAULT_FIELDS
    assert cfg.token == conn.DEFAULT_TOKEN


def test_load_reads_env_mode_with_explicit_fields_and_token(tmp_path):
    _write_config(tmp_path, mode="env", fields=["url", "wcs"], token="<<{key}>>")
    cfg = ProjectConfig.load(tmp_path)
    assert cfg.mode == "env"
    assert cfg.is_env is True
    assert tuple(cfg.fields) == ("url", "wcs")
    assert cfg.token == "<<{key}>>"


# --------------------------------------------------------------------------- #
# Strict resolution (ADR-0001): no aprx.json / no mode -> "run aprx install"
# --------------------------------------------------------------------------- #

def test_load_missing_config_directs_to_install(tmp_path):
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert "aprx install" in str(exc.value)


def test_load_config_without_mode_directs_to_install(tmp_path):
    _write_config(tmp_path, fields=["url"])  # has config, but no mode
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert "aprx install" in str(exc.value)


def test_load_rejects_unknown_mode(tmp_path):
    _write_config(tmp_path, mode="production")
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert "production" in str(exc.value)


def test_load_rejects_token_without_key_placeholder(tmp_path):
    _write_config(tmp_path, mode="env", token="@@no-placeholder@@")
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert "{key}" in str(exc.value)


def test_load_rejects_malformed_json(tmp_path):
    # A merge-conflict / typo'd aprx.json must fail loudly with our diagnostic,
    # not a raw json.JSONDecodeError traceback.
    (tmp_path / conn.CONFIG_FILENAME).write_text("{not: valid", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert str(tmp_path / conn.CONFIG_FILENAME) in str(exc.value)


@pytest.mark.parametrize("payload", ["42", "null", '"simple"', "[]"])
def test_load_rejects_non_object_config(tmp_path, payload):
    # A top-level scalar/list must not crash with TypeError or be misread as
    # "declares no mode" — it is simply not a valid config object.
    (tmp_path / conn.CONFIG_FILENAME).write_text(payload, encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert "JSON object" in str(exc.value)


def test_load_rejects_string_fields(tmp_path):
    # A string `fields` would be shredded into single characters by tuple(),
    # silently matching nothing and leaking raw connection strings. Reject it.
    _write_config(tmp_path, mode="env", fields="workspaceConnectionString")
    with pytest.raises(SystemExit) as exc:
        ProjectConfig.load(tmp_path)
    assert "fields" in str(exc.value)


# --------------------------------------------------------------------------- #
# Environment mode: connection-file discovery + map building
# --------------------------------------------------------------------------- #

def _env_project(tmp_path, dev=None, uat=None, local=None):
    """An env-mode project dir with aprx.json + the given connection files."""
    _write_config(tmp_path, mode="env")
    (tmp_path / conn.CONNECTIONS_DIR).mkdir()
    if dev is not None:
        (tmp_path / conn.CONNECTIONS_DIR / "dev.json").write_text(json.dumps(dev))
    if uat is not None:
        (tmp_path / conn.CONNECTIONS_DIR / "uat.json").write_text(json.dumps(uat))
    if local is not None:
        (tmp_path / conn.LOCAL_FILE).write_text(json.dumps(local))
    return tmp_path


def test_env_forward_map_for_chosen_environment(tmp_path):
    _env_project(tmp_path, dev={"main": "DEV"}, uat={"main": "UAT"}, local={"main": "DEV"})
    cfg = ProjectConfig.load(tmp_path)
    # token key -> value for the named environment
    assert cfg.forward_map(env="uat") == {"main": "UAT"}
    # default (no flag) falls back to local.json
    assert cfg.forward_map() == {"main": "DEV"}


# --------------------------------------------------------------------------- #
# Mode is the master switch: env-only helpers reject a simple-mode project
# --------------------------------------------------------------------------- #

def test_simple_mode_rejects_substitution_helpers(tmp_path):
    # Even with a stray local.json present, a simple-mode project must not
    # expose connection maps — substitution is an environment-mode concept.
    _write_config(tmp_path, mode="simple")
    (tmp_path / conn.LOCAL_FILE).write_text(json.dumps({"main": "X"}))
    cfg = ProjectConfig.load(tmp_path)
    for call in (cfg.committed_reverse_map, cfg.forward_map, cfg.connection_key_sets):
        with pytest.raises(SystemExit) as exc:
            call()
        assert "simple" in str(exc.value)


# --------------------------------------------------------------------------- #
# Domain questions over a Project's entries (issue 0003)
#
# These are the checks verify and bootstrap used to hand-assemble from the
# connections engine. They take the parsed JSON entries the Entry reader yields,
# so they unit-test by constructing a config and passing a list of parsed dicts —
# no temp .aprx, no directory walk.
# --------------------------------------------------------------------------- #

WCS = "workspaceConnectionString"


def _env_cfg(tmp_path, fields=(WCS,), token="@@{key}@@"):
    """An env-mode ProjectConfig built straight from values — the scan methods that
    take parsed entries need no aprx.json on disk."""
    return ProjectConfig(dir=tmp_path, mode="env", fields=fields, token=token)


def _entry(value):
    """A parsed JSON entry nesting the connection field, as the Entry reader yields."""
    return {"layer": {"dataConnection": {WCS: value, "dataset": "x"}}}


def test_referenced_keys_collects_every_token(tmp_path):
    cfg = _env_cfg(tmp_path)
    assert cfg.referenced_keys([_entry("@@main@@"), _entry("@@aux@@")]) == {"main", "aux"}


def test_referenced_keys_honours_custom_token_and_fields(tmp_path):
    cfg = _env_cfg(tmp_path, fields=("url",), token="<<{key}>>")
    assert cfg.referenced_keys([{"url": "<<sde>>"}, {WCS: "<<ignored>>"}]) == {"sde"}


def test_leaked_values_flags_raw_strings(tmp_path):
    cfg = _env_cfg(tmp_path)
    assert cfg.leaked_values([_entry("@@main@@"), _entry("DB=leaked")]) == {"DB=leaked"}


def test_leaked_values_empty_when_fully_tokenised(tmp_path):
    cfg = _env_cfg(tmp_path)
    assert cfg.leaked_values([_entry("@@main@@"), _entry("@@aux@@")]) == set()


def test_unresolved_keys_reports_what_env_does_not_cover(tmp_path):
    (tmp_path / "uat.json").write_text(json.dumps({"main": "X"}))
    cfg = _env_cfg(tmp_path)
    parsed = [_entry("@@main@@"), _entry("@@aux@@")]
    assert cfg.unresolved_keys(parsed, tmp_path / "uat.json") == {"aux"}


def test_unresolved_keys_empty_when_env_covers_all(tmp_path):
    (tmp_path / "uat.json").write_text(json.dumps({"main": "X", "aux": "Y"}))
    cfg = _env_cfg(tmp_path)
    parsed = [_entry("@@main@@"), _entry("@@aux@@")]
    assert cfg.unresolved_keys(parsed, tmp_path / "uat.json") == set()


def test_discovered_values_collects_distinct_strings(tmp_path):
    cfg = _env_cfg(tmp_path)
    parsed = [_entry("DB=a"), _entry("DB=b"), _entry("DB=a")]
    assert cfg.discovered_values(parsed) == {"DB=a", "DB=b"}


def test_connection_key_sets_maps_each_env_to_its_keys(tmp_path):
    (tmp_path / conn.CONNECTIONS_DIR).mkdir()
    (tmp_path / conn.CONNECTIONS_DIR / "dev.json").write_text(json.dumps({"main": "D"}))
    (tmp_path / conn.CONNECTIONS_DIR / "uat.json").write_text(
        json.dumps({"main": "U", "aux": "U2"})
    )
    cfg = _env_cfg(tmp_path)
    assert cfg.connection_key_sets() == {
        "dev.json": {"main"},
        "uat.json": {"main", "aux"},
    }
