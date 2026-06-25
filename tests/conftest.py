import json
import shutil
import types
import zipfile

import pytest
from pathlib import Path

FIXTURES_DIR = Path(__file__).parent / "fixtures"
SIMPLE_APRX = FIXTURES_DIR / "simple" / "simple.aprx"


@pytest.fixture
def simple_aprx() -> Path:
    return SIMPLE_APRX


@pytest.fixture
def deny_reading(monkeypatch):
    """Make exactly one file raise ``PermissionError`` on read, leaving every other file
    readable — a root-safe stand-in for ``chmod 000`` (CI may run as root, where the file
    mode is ignored). Models issue 0005's "present but unreadable ``aprx.json``".

    Matches on the **resolved** path, so it denies only the named Project's config and
    never a sibling's (the hook resolves a Project dir through ``git_root``, so a basename
    match would also block other Projects and could mask a per-Project regression)."""
    def _deny(target) -> None:
        target = Path(target).resolve()
        real_read_text = Path.read_text

        def guarded(self, *args, **kwargs):
            if self.resolve() == target:
                raise PermissionError(13, "Permission denied")
            return real_read_text(self, *args, **kwargs)

        monkeypatch.setattr(Path, "read_text", guarded)

    return _deny


@pytest.fixture
def exploded(tmp_path, simple_aprx) -> Path:
    """Exploded simple.aprx — reused by pack and compare tests."""
    from aprx_tools.explode import explode
    return explode(str(simple_aprx), str(tmp_path / "simple.aprx.src"))


def _first_connection_value(aprx: Path) -> str:
    """The actual workspaceConnectionString stored in the fixture."""
    from aprx_tools.connections import collect_field_values
    with zipfile.ZipFile(aprx) as zf:
        for name in zf.namelist():
            if name.endswith(".json"):
                vals = collect_field_values(json.loads(zf.read(name)))
                if vals:
                    return sorted(vals)[0]
    raise AssertionError("fixture has no connection strings")


@pytest.fixture
def env_project(tmp_path, simple_aprx):
    """An environment-managed project: an `aprx.json` declaring `mode: env`, plus
    map.aprx and connections/{dev,uat}.json and local.json, with `dev` and `local`
    pointing at the fixture's real connection string and `uat` at a distinct one.

    The `aprx.json` is required now that resolution is strict (ADR-0001): without a
    declared mode `ProjectConfig.load` errors."""
    proj = tmp_path / "proj"
    (proj / "connections").mkdir(parents=True)
    aprx = proj / "map.aprx"
    shutil.copy(simple_aprx, aprx)

    (proj / "aprx.json").write_text(json.dumps({"mode": "env"}))
    value = _first_connection_value(aprx)
    uat_value = "DATABASE=uat-server;SERVER=uat"
    (proj / "connections" / "dev.json").write_text(json.dumps({"main": value}))
    (proj / "connections" / "uat.json").write_text(json.dumps({"main": uat_value}))
    (proj / "local.json").write_text(json.dumps({"main": value}))

    return types.SimpleNamespace(dir=proj, aprx=aprx, value=value, uat_value=uat_value)


@pytest.fixture
def explode_env():
    """Explode an environment-mode Project the way the CLI composition root does:
    load its `ProjectConfig` and inject `Substitution.for_explode`.

    The pure `explode` defaults to `IDENTITY`; environment tokenisation is the
    composition root's job (ADR-0002), so integration tests that want neutral source
    go through this helper rather than calling `explode` bare. It routes through the
    same `transform.explode_transform` the CLI uses (mode-selected), so the tests
    exercise the real composition root, not a re-implementation. `project_dir` defaults
    to the directory holding the .aprx (where its `aprx.json` lives)."""
    from aprx_tools.explode import explode
    from aprx_tools.transform import explode_transform

    def _go(aprx, project_dir=None, output_dir=None):
        transform = explode_transform(project_dir or Path(aprx).parent)
        return explode(str(aprx), output_dir, transform=transform)

    return _go


@pytest.fixture
def pack_env():
    """Pack an environment-mode Project the way the CLI composition root does:
    load its `ProjectConfig` and inject `Substitution.for_pack` for the chosen
    environment (the mirror of `explode_env`).

    The pure `pack` defaults to `IDENTITY`; choosing an environment's connection
    values is the composition root's job (ADR-0002), so integration tests that want a
    substituted build go through this helper, which routes through the same
    `transform.pack_transform` the CLI uses (mode-selected, precedence
    `connections_file` > `env` > `local.json`). `project_dir` defaults to the parent of
    the src dir — the project directory where `aprx.json` lives."""
    from aprx_tools.pack import pack
    from aprx_tools.transform import pack_transform

    def _go(src, output=None, project_dir=None, env=None, connections_file=None):
        transform = pack_transform(project_dir or Path(src).resolve().parent,
                                   env=env, connections_file=connections_file)
        return pack(str(src), str(output) if output else None, transform=transform)

    return _go


# A synthetic .aprx (a plain zip of JSON) carrying several *distinct* connection
# strings of different shapes — the fixture has only one, so this exercises the
# multiple-keys path through explode / connections init without a real Pro export.
MULTI_CONN_VALUES = {
    "local":  r"DATABASE=.\acme.gdb",
    "parcels": r"\\fileserver\gis\parcels.gdb",
    "acme_prod": "SERVER=gisdb;INSTANCE=sde:postgresql:gisdb;DATABASE=acme_prod;USER=viewer",
}


@pytest.fixture
def multi_conn_aprx(tmp_path):
    proj = tmp_path / "multi"
    proj.mkdir()
    aprx = proj / "multi.aprx"
    entries = {
        "GISProject.json": {"version": "3.0"},
        "map/a.json": {"dataConnection": {"workspaceConnectionString": MULTI_CONN_VALUES["local"], "dataset": "a"}},
        "map/b.json": {"dataConnection": {"workspaceConnectionString": MULTI_CONN_VALUES["parcels"], "dataset": "b"}},
        "map/c.json": {"layer": {"dataConnection": {"workspaceConnectionString": MULTI_CONN_VALUES["acme_prod"], "dataset": "c"}}},
    }
    with zipfile.ZipFile(aprx, "w") as zf:
        for name, obj in entries.items():
            zf.writestr(name, json.dumps(obj))
    return types.SimpleNamespace(dir=proj, aprx=aprx, values=set(MULTI_CONN_VALUES.values()))
