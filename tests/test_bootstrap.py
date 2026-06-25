import json

import pytest

from aprx_tools.bootstrap import _suggest_key, connections_init, connections_check
from aprx_tools.project_config import ProjectConfig


# --------------------------------------------------------------------------- #
# _suggest_key — the only place connection-string *content* is parsed
# --------------------------------------------------------------------------- #

@pytest.mark.parametrize("value, expected", [
    (r"DATABASE=.\acme.gdb",                       "acme"),       # local file gdb
    (r"\\fileserver\gis\parcels.gdb",             "parcels"),   # UNC path
    (r"C:\connections\prod_sql.sde",              "prod_sql"),  # .sde reference
    ("SERVER=db;INSTANCE=sde:postgresql:db;DATABASE=acme_prod;USER=v", "acme_prod"),  # enterprise
    (".\\Café_Data.gdb",                          "caf_data"),  # non-ascii slugged out
])
def test_suggest_key_derives_from_shape(value, expected):
    assert _suggest_key(value, set()) == expected


def test_suggest_key_falls_back_for_opaque_values():
    # A URL has no .gdb/.sde or DATABASE= to key off → positional fallback.
    url = "http://services.arcgisonline.com/ArcGIS/rest/services/World/MapServer"
    assert _suggest_key(url, set()) == "conn1"
    assert _suggest_key(url, {"conn1"}) == "conn2"


def test_suggest_key_disambiguates_collisions():
    taken = set()
    first = _suggest_key(r"a\data.gdb", taken); taken.add(first)
    second = _suggest_key(r"b\data.gdb", taken); taken.add(second)
    assert first == "data"
    assert second == "data_2"


# --------------------------------------------------------------------------- #
# connections init — discovers every distinct string and scaffolds files
# --------------------------------------------------------------------------- #

def test_init_discovers_all_distinct_connections(multi_conn_aprx, capsys):
    connections_init(str(multi_conn_aprx.aprx))

    dev = json.loads((multi_conn_aprx.dir / "connections" / "dev.json").read_text())
    assert set(dev.values()) == multi_conn_aprx.values          # all three captured
    assert len(dev) == 3                                        # as three distinct keys
    assert (multi_conn_aprx.dir / "aprx.json").exists()
    assert (multi_conn_aprx.dir / "local.json.example").exists()


def test_init_then_explode_tokenizes_each_distinctly(multi_conn_aprx, explode_env):
    connections_init(str(multi_conn_aprx.aprx))
    # use the discovered dev mapping as the local working config
    dev = (multi_conn_aprx.dir / "connections" / "dev.json").read_text()
    (multi_conn_aprx.dir / "local.json").write_text(dev)
    # `connections init` records `mode: env` itself, so the composition root resolves
    # a Substitution with no extra setup — explode straight away.
    src = explode_env(multi_conn_aprx.aprx)

    blob = "".join(p.read_text() for p in (src / "map").glob("*.json"))
    for value in multi_conn_aprx.values:
        assert value not in blob                                # no raw strings leak
    keys = json.loads(dev).keys()
    for key in keys:
        assert f"@@{key}@@" in blob                             # each became its own token


def test_init_refuses_when_no_connections(tmp_path):
    import zipfile
    empty = tmp_path / "empty.aprx"
    with zipfile.ZipFile(empty, "w") as zf:
        zf.writestr("GISProject.json", json.dumps({"version": "3.0"}))
    with pytest.raises(SystemExit):
        connections_init(str(empty))


# --------------------------------------------------------------------------- #
# connections init records mode: env (issue 0008)
# --------------------------------------------------------------------------- #

def test_init_records_env_mode_resolvable_by_projectconfig(multi_conn_aprx):
    # AC1/AC2: init writes `mode: env`, and ProjectConfig.load resolves it as such.
    connections_init(str(multi_conn_aprx.aprx))

    cfg_raw = json.loads((multi_conn_aprx.dir / "aprx.json").read_text())
    assert cfg_raw["mode"] == "env"

    cfg = ProjectConfig.load(multi_conn_aprx.dir)
    assert cfg.is_env


def test_init_refuses_to_switch_a_declared_simple_mode(multi_conn_aprx):
    # The user-chosen rule: a committed `mode: simple` is a deliberate decision, so
    # init refuses to silently flip it to env and points at the manual upgrade.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_text(json.dumps({"mode": "simple"}))

    with pytest.raises(SystemExit) as exc:
        connections_init(str(multi_conn_aprx.aprx))
    msg = str(exc.value)
    assert "simple" in msg and "mode" in msg          # names the conflict + how to fix
    assert json.loads(cfg_path.read_text())["mode"] == "simple"   # left untouched


def test_init_preserves_fields_token_from_modeless_legacy_config(multi_conn_aprx):
    # Regression: a legacy aprx.json the *previous* `connections init` scaffolded
    # (fields + token, no mode). Re-running init declares env mode without clobbering
    # a custom token back to the engine default.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_text(json.dumps(
        {"fields": ["workspaceConnectionString"], "token": "T-{key}"}
    ))

    connections_init(str(multi_conn_aprx.aprx))

    cfg = json.loads(cfg_path.read_text())
    assert cfg["mode"] == "env"                 # mode now declared
    assert cfg["token"] == "T-{key}"            # custom token preserved, not defaulted
    assert cfg["fields"] == ["workspaceConnectionString"]


# --------------------------------------------------------------------------- #
# init reads an existing aprx.json before a ProjectConfig can exist; that bootstrap
# read must fail-graceful too (issue 0010). Unlike install — which swallows and writes
# a fresh config — init is a user-invoked one-shot, so it routes through
# conn.read_json_or_exit for the same three-way directed diagnostic the resolution
# path (0005) and the connections files (0009) already give.
# --------------------------------------------------------------------------- #

def test_init_unreadable_config_reports_io_not_content(multi_conn_aprx, deny_reading):
    # Present but unreadable (chmod 000 / transient I/O). Denied via monkeypatch so it
    # holds even as root, where the file mode is ignored.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_text(json.dumps({"mode": "env"}), encoding="utf-8")
    deny_reading(cfg_path)

    with pytest.raises(SystemExit) as exc:
        connections_init(str(multi_conn_aprx.aprx))
    msg = str(exc.value)
    assert str(cfg_path) in msg
    assert "could not be read" in msg          # an I/O problem...
    assert "permission" in msg.lower()
    assert "valid JSON" not in msg             # ...not the malformed-content diagnostic


def test_init_non_utf8_config_reports_encoding(multi_conn_aprx):
    # A non-UTF-8 aprx.json makes read_text raise UnicodeDecodeError — a ValueError, not
    # an OSError — which the bare json.loads(read_text()) would let crash. Routed through
    # read_json_or_exit, it becomes the distinct encoding diagnostic.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_bytes(b"\xff\xfe{ not utf-8 ")

    with pytest.raises(SystemExit) as exc:
        connections_init(str(multi_conn_aprx.aprx))
    msg = str(exc.value)
    assert "UTF-8" in msg
    assert "could not be read" not in msg      # distinct from the I/O message
    assert "valid JSON" not in msg             # distinct from the malformed-JSON message


def test_init_malformed_config_reports_json(multi_conn_aprx):
    # A hand-broken / merge-conflicted aprx.json fails with our diagnostic, not a raw
    # json.JSONDecodeError traceback.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_text("{ not valid json", encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        connections_init(str(multi_conn_aprx.aprx))
    msg = str(exc.value)
    assert "valid JSON" in msg
    assert "could not be read" not in msg
    assert "UTF-8" not in msg


@pytest.mark.parametrize("payload", ["[]", "null", '"simple"', "42"])
def test_init_non_object_config_reports_shape(multi_conn_aprx, payload):
    # read_json_or_exit deliberately does no shape check; init must pair it with one
    # (as load_connections does) so a valid-JSON-but-non-object aprx.json fails with a
    # directed message instead of `existing.get("mode")` raising a raw AttributeError.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_text(payload, encoding="utf-8")

    with pytest.raises(SystemExit) as exc:
        connections_init(str(multi_conn_aprx.aprx))
    msg = str(exc.value)
    assert str(cfg_path) in msg
    assert "JSON object" in msg


def test_init_preserves_a_declared_env_config(multi_conn_aprx):
    # Compose with `aprx install --mode env`: when env mode (and custom token) is
    # already declared, init sources fields/token from ProjectConfig and keeps them
    # — mirroring install's preserve test for the init side.
    cfg_path = multi_conn_aprx.dir / "aprx.json"
    cfg_path.write_text(json.dumps(
        {"mode": "env", "token": "T-{key}", "fields": ["workspaceConnectionString"]}
    ))

    connections_init(str(multi_conn_aprx.aprx))

    cfg = json.loads(cfg_path.read_text())
    assert cfg["mode"] == "env"
    assert cfg["token"] == "T-{key}"
    assert cfg["fields"] == ["workspaceConnectionString"]


# --------------------------------------------------------------------------- #
# connections check — every environment defines the same keys (issue 0008)
# --------------------------------------------------------------------------- #

def test_check_passes_when_keys_match(env_project, monkeypatch, capsys):
    # AC3: env_project's dev.json and uat.json both define `main` → exit 0.
    monkeypatch.chdir(env_project.dir)
    with pytest.raises(SystemExit) as exc:
        connections_check()
    assert exc.value.code == 0
    assert "same" in capsys.readouterr().out.lower()


def test_check_fails_listing_gaps(env_project, monkeypatch, capsys):
    # AC3: drop `main` from uat so the environments disagree → exit 1 naming the gap.
    (env_project.dir / "connections" / "uat.json").write_text(json.dumps({}))
    monkeypatch.chdir(env_project.dir)
    with pytest.raises(SystemExit) as exc:
        connections_check()
    assert exc.value.code == 1
    out = capsys.readouterr().out
    assert "uat.json" in out and "main" in out


def test_check_rejects_simple_mode_project(tmp_path, monkeypatch):
    # check is an environment-mode operation; routing through ProjectConfig means a
    # simple-mode project is rejected rather than silently checking nothing.
    (tmp_path / "aprx.json").write_text(json.dumps({"mode": "simple"}))
    monkeypatch.chdir(tmp_path)
    with pytest.raises(SystemExit) as exc:
        connections_check()
    assert exc.value.code != 0
