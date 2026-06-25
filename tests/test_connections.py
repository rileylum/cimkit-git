import json
import zipfile

import pytest

from aprx_tools import connections as conn
from aprx_tools.explode import explode
from aprx_tools.compare import compare
from aprx_tools.transform import SubstitutionError


# --------------------------------------------------------------------------- #
# Engine: tokenize / substitute
# --------------------------------------------------------------------------- #

def _doc(value):
    return {"layers": [{"dataConnection": {"workspaceConnectionString": value,
                                           "dataset": "pts"}}]}


def _conn_str(doc):
    return doc["layers"][0]["dataConnection"]["workspaceConnectionString"]


def test_tokenize_then_substitute_roundtrip():
    doc = _doc("DATABASE=.\\acme.gdb")
    tokenized, unknown = conn.tokenize(doc, {"DATABASE=.\\acme.gdb": "main"})
    assert _conn_str(tokenized) == "@@main@@"
    assert unknown == set()

    restored, missing = conn.substitute(tokenized, {"main": "SERVER=uat;DATABASE=acme"})
    assert _conn_str(restored) == "SERVER=uat;DATABASE=acme"
    assert missing == set()


def test_tokenize_flags_unknown_value():
    _, unknown = conn.tokenize(_doc("DATABASE=mystery"), {"DATABASE=known": "k"})
    assert unknown == {"DATABASE=mystery"}


def test_tokenize_leaves_existing_token():
    doc, unknown = conn.tokenize(_doc("@@main@@"), {"DATABASE=x": "main"})
    assert _conn_str(doc) == "@@main@@"
    assert unknown == set()


def test_substitute_flags_missing_key():
    _, missing = conn.substitute(_doc("@@nope@@"), {"main": "x"})
    assert missing == {"nope"}


def test_only_configured_fields_are_touched():
    doc = {"workspaceConnectionString": "v", "somethingElse": "v"}
    tokenized, _ = conn.tokenize(doc, {"v": "k"})
    assert tokenized["workspaceConnectionString"] == "@@k@@"
    assert tokenized["somethingElse"] == "v"  # untouched


def test_configurable_fields():
    doc = {"serviceUrl": "https://dev/svc"}
    tokenized, _ = conn.tokenize(doc, {"https://dev/svc": "svc"}, fields=("serviceUrl",))
    assert tokenized["serviceUrl"] == "@@svc@@"


@pytest.mark.parametrize("value", [
    "DATABASE=C:\\data\\acme.gdb",                       # Windows path (backslashes)
    "\\\\fileserver\\gis\\parcels.gdb",                 # UNC path
    "SERVER=db;INSTANCE=sde:sqlserver:db;DATABASE=acme",  # enterprise (; and =)
    'PASSWORD="a;b\\c";USER=v',                          # embedded quote/backslash/semicolon
    "DATABASE=.\\café_районexplore.gdb",                # non-ASCII
])
def test_special_chars_survive_substitution_roundtrip(value):
    # Operating on parsed JSON (not text) means any value re-serialises validly and
    # comes back byte-identical through a real json dump/load cycle.
    restored, missing = conn.substitute(_doc("@@main@@"), {"main": value})
    assert missing == set()
    reparsed = json.loads(json.dumps(restored, ensure_ascii=False))
    assert _conn_str(reparsed) == value
    # and the reverse direction recognises it
    tokenized, unknown = conn.tokenize(_doc(value), {value: "main"})
    assert _conn_str(tokenized) == "@@main@@"
    assert unknown == set()


# --------------------------------------------------------------------------- #
# Engine: shared traversal reaches deeply-nested configured fields
# --------------------------------------------------------------------------- #

def _nested(value):
    """A configured field buried under dict -> list -> dict -> list -> dict, so the
    shared walk has to descend several mixed dict/list levels to reach it."""
    return {"a": [{"b": {"c": [{"workspaceConnectionString": value, "keep": value}]}}]}


def _nested_conn(doc):
    return doc["a"][0]["b"]["c"][0]["workspaceConnectionString"]


def test_traversal_tokenizes_nested_field():
    doc, unknown = conn.tokenize(_nested("DB=deep"), {"DB=deep": "main"})
    assert _nested_conn(doc) == "@@main@@"
    assert unknown == set()
    # the non-configured sibling key is left raw even at depth
    assert doc["a"][0]["b"]["c"][0]["keep"] == "DB=deep"


def test_traversal_substitutes_nested_field():
    doc, missing = conn.substitute(_nested("@@main@@"), {"main": "DB=real"})
    assert _nested_conn(doc) == "DB=real"
    assert missing == set()


def test_traversal_collects_nested_field():
    assert conn.collect_field_values(_nested("DB=deep")) == {"DB=deep"}


def test_traversal_scans_nested_field():
    keys, raw = conn.scan_tokens(_nested("@@main@@"))
    assert keys == {"main"}
    assert raw == set()
    keys, raw = conn.scan_tokens(_nested("DB=leaked"))
    assert keys == set()
    assert raw == {"DB=leaked"}


# --------------------------------------------------------------------------- #
# Engine: reverse map
# --------------------------------------------------------------------------- #

def test_reverse_map_unions_envs(tmp_path):
    (tmp_path / "dev.json").write_text(json.dumps({"main": "DEV"}))
    (tmp_path / "uat.json").write_text(json.dumps({"main": "UAT"}))
    rev = conn.build_reverse_map([tmp_path / "dev.json", tmp_path / "uat.json"])
    assert rev == {"DEV": "main", "UAT": "main"}


def test_reverse_map_value_collision_errors(tmp_path):
    (tmp_path / "a.json").write_text(json.dumps({"main": "SAME"}))
    (tmp_path / "b.json").write_text(json.dumps({"other": "SAME"}))
    with pytest.raises(SystemExit):
        conn.build_reverse_map([tmp_path / "a.json", tmp_path / "b.json"])


# --------------------------------------------------------------------------- #
# Engine: load_connections fails gracefully on an unreadable file (issue 0009)
#
# load_connections is the engine's one file-read boundary; an uncaught OSError /
# UnicodeDecodeError / JSONDecodeError here would propagate out of build_reverse_map,
# explode_transform and the pre-commit sweep — crashing the whole commit one read site
# over from 0005's aprx.json fix. Each is converted to a *distinct* sys.exit, the same
# three-way split ProjectConfig.load gives aprx.json, so the user knows whether it is a
# permissions/IO, an encoding, or a content problem.
# --------------------------------------------------------------------------- #

def test_load_unreadable_connections_reports_io_not_content(tmp_path, deny_reading):
    # Present but unreadable (chmod 000, a transient I/O error). Denied via monkeypatch
    # (not chmod) so it holds even when the suite runs as root, where the mode is ignored.
    path = tmp_path / "dev.json"
    path.write_text(json.dumps({"main": "DEV"}), encoding="utf-8")
    deny_reading(path)

    with pytest.raises(SystemExit) as exc:
        conn.load_connections(path)
    msg = str(exc.value)
    assert str(path) in msg
    assert "could not be read" in msg          # an I/O problem...
    assert "permission" in msg.lower()
    assert "JSON" not in msg                    # ...not the malformed-content diagnostic


def test_load_non_utf8_connections_reports_encoding_not_io(tmp_path):
    # A non-UTF-8 connections file (saved UTF-16/Latin-1, or a stray 0xFF byte) makes
    # read_text raise UnicodeDecodeError — a ValueError, *not* an OSError — which would
    # sail past both the OSError and JSONDecodeError guards and crash the sweep.
    path = tmp_path / "dev.json"
    path.write_bytes(b"\xff\xfe{ not utf-8 ")
    with pytest.raises(SystemExit) as exc:
        conn.load_connections(path)
    msg = str(exc.value)
    assert str(path) in msg
    assert "UTF-8" in msg
    assert "could not be read" not in msg       # distinct from the I/O diagnostic


def test_load_malformed_connections_reports_json(tmp_path):
    # A hand-broken / merge-conflicted connections file must fail with our diagnostic,
    # not a raw json.JSONDecodeError traceback.
    path = tmp_path / "dev.json"
    path.write_text("{not: valid", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        conn.load_connections(path)
    msg = str(exc.value)
    assert str(path) in msg
    assert "valid JSON" in msg


# --------------------------------------------------------------------------- #
# Engine: resolution precedence
# --------------------------------------------------------------------------- #

def test_resolve_explicit_connections_file(env_project):
    p = conn.resolve_connections_file(env_project.dir, None,
                                      str(env_project.dir / "local.json"))
    assert p.name == "local.json"


def test_resolve_env(env_project):
    p = conn.resolve_connections_file(env_project.dir, "uat", None)
    assert p == env_project.dir / "connections" / "uat.json"


def test_resolve_default_local(env_project):
    p = conn.resolve_connections_file(env_project.dir, None, None)
    assert p.name == "local.json"


def test_resolve_none_when_no_local(tmp_path):
    assert conn.resolve_connections_file(tmp_path, None, None) is None


def test_resolve_missing_explicit_errors(env_project):
    with pytest.raises(SystemExit):
        conn.resolve_connections_file(env_project.dir, None, "nope.json")


def test_resolve_missing_env_errors(env_project):
    with pytest.raises(SystemExit):
        conn.resolve_connections_file(env_project.dir, "prd", None)


# --------------------------------------------------------------------------- #
# Integration: explode + pack through a project
# --------------------------------------------------------------------------- #

def _packed_layer(out):
    with zipfile.ZipFile(out) as zf:
        return zf.read("map/test_points.json").decode("utf-8")


def test_explode_tokenizes(env_project, explode_env):
    src = explode_env(env_project.aprx)
    pts = (src / "map" / "test_points.json").read_text()
    assert "@@main@@" in pts
    assert env_project.value not in pts


def test_pack_substitutes_for_env(env_project, explode_env, pack_env):
    src = explode_env(env_project.aprx)
    out = pack_env(src, output=env_project.dir / "map.uat.aprx", env="uat")
    data = _packed_layer(out)
    assert env_project.uat_value in data
    assert "@@main@@" not in data


def test_roundtrip_identical_with_local(env_project, explode_env, pack_env):
    src = explode_env(env_project.aprx)
    rebuilt = pack_env(src, output=env_project.dir / "rebuilt.aprx",
                       connections_file=str(env_project.dir / "local.json"))
    assert compare(str(env_project.aprx), str(rebuilt)) is False


def test_pack_missing_key_fails(env_project, explode_env, pack_env):
    src = explode_env(env_project.aprx)
    (env_project.dir / "connections" / "broken.json").write_text("{}")
    # The transform raises SubstitutionError (the seam owns the wording now, ADR-0002).
    with pytest.raises(SubstitutionError):
        pack_env(src, output=env_project.dir / "x.aprx", env="broken")


def test_explode_unknown_connection_fails(env_project, explode_env):
    # Point every committed connection file away from the fixture's real value.
    for name in ("dev.json", "uat.json"):
        (env_project.dir / "connections" / name).write_text(json.dumps({"main": "OTHER"}))
    (env_project.dir / "local.json").write_text(json.dumps({"main": "OTHER"}))
    # The transform raises SubstitutionError (the seam owns the wording now, ADR-0002).
    with pytest.raises(SubstitutionError):
        explode_env(env_project.aprx)


def test_simple_mode_untouched(tmp_path, simple_aprx):
    # No connection files anywhere → explode keeps the raw connection string.
    src = explode(str(simple_aprx), str(tmp_path / "simple.aprx.src"))
    pts = (src / "map" / "test_points.json").read_text()
    assert "@@" not in pts
    assert ".gdb" in pts


def test_configurable_field_substitutes_service_urls(tmp_path, simple_aprx, explode_env):
    # The basemap stores its endpoint under `url`, not workspaceConnectionString.
    # Declaring `fields: ["url"]` in aprx.json makes those substitutable too —
    # the "URLs live under a different field" case, on the existing fixture.
    import shutil
    proj = tmp_path / "u"
    (proj / "connections").mkdir(parents=True)
    aprx = proj / "map.aprx"
    shutil.copy(simple_aprx, aprx)

    urls = set()
    with zipfile.ZipFile(aprx) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                urls |= conn.collect_field_values(json.loads(zf.read(name)), fields=("url",))
    assert urls, "fixture should contain service URLs"

    mapping = {f"svc{i}": v for i, v in enumerate(sorted(urls))}
    (proj / "aprx.json").write_text(json.dumps({"mode": "env", "fields": ["url"]}))
    (proj / "connections" / "dev.json").write_text(json.dumps(mapping))
    (proj / "local.json").write_text(json.dumps(mapping))

    src = explode_env(aprx)
    topo = (src / "map" / "topographic.json").read_text()
    for value in urls:
        assert value not in topo
    assert "@@svc0@@" in topo
    # workspaceConnectionString is NOT in `fields` here, so it stays raw.
    assert ".gdb" in (src / "map" / "test_points.json").read_text()
