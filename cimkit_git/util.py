import subprocess
import sys
from pathlib import Path


def git_root(start: Path = None, *, required: bool = True) -> Path:
    """The repository top-level (``git rev-parse --show-toplevel``) for *start*
    (default: the current directory).

    One mechanism, two policies — parameterised on ``required`` because the callers
    genuinely differ:

    - ``required=True`` (``install``): a repo is mandatory — there is no ``.git`` to
      write hooks into otherwise — so git failing *or* being absent is a hard exit.
    - ``required=False`` (``verify``): degrade gracefully — fall back to *start* so
      the CI gate still runs (and simply finds no source) when invoked outside a repo
      or where git is unavailable.
    """
    start = Path.cwd() if start is None else start
    try:
        out = subprocess.check_output(
            ["git", "rev-parse", "--show-toplevel"],
            cwd=start, text=True, stderr=subprocess.DEVNULL,
        )
        return Path(out.strip())
    except (subprocess.CalledProcessError, FileNotFoundError):
        if required:
            sys.exit("cimkit-git: not inside a git repository")
        return start


def src_dir_for(aprx_path: Path) -> Path:
    """map.aprx → map.aprx.src  (adjacent to the .aprx file)."""
    return aprx_path.parent / (aprx_path.name + ".src")


def aprx_for_src_dir(src_dir: Path) -> Path:
    """map.aprx.src → map.aprx  (Path.stem strips the last extension).

    Strict: the input *must* follow the convention. Use this where a violation is a
    bug (hooks, verify). For best-effort naming of an arbitrary directory the user
    points a command at, use ``aprx_output_for``."""
    if not src_dir.name.endswith(".aprx.src"):
        raise ValueError(f"{src_dir.name!r} does not follow the <name>.aprx.src convention")
    return src_dir.parent / src_dir.stem


def aprx_output_for(src_dir: Path) -> Path:
    """The .aprx to write when packing *src_dir* — the lenient sibling of
    ``aprx_for_src_dir``. Commands the user can aim at any directory (``pack``,
    ``build``) accept a folder that may not follow our convention, so this never
    raises: strip a single trailing ``.src`` if present, then ensure an ``.aprx``
    extension.

        map.aprx.src → map.aprx    data.src → data.aprx    myfolder → myfolder.aprx
    """
    name = src_dir.name
    if name.endswith(".src"):
        name = name[: -len(".src")]
    if not name.endswith(".aprx"):
        name += ".aprx"
    return src_dir.parent / name


def is_aprx_src_dir(path: Path) -> bool:
    return (
        path.is_dir()
        and path.name.endswith(".aprx.src")
        and (path / "GISProject.json").exists()
    )


def iter_src_dirs(root: Path):
    """Yield every .aprx.src directory under *root* (located via GISProject.json)."""
    for gis_project in Path(root).rglob("GISProject.json"):
        src_dir = gis_project.parent
        if is_aprx_src_dir(src_dir):
            yield src_dir
