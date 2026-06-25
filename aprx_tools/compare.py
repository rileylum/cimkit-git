import difflib
from pathlib import Path

from .entry import normalise, read_entries


def compare(path_a: str, path_b: str) -> bool:
    """Compare two .aprx files or directories semantically.

    Returns True if any differences were found.
    """
    pa, pb = Path(path_a), Path(path_b)
    # The Entry reader (issue 0002) owns reading a Project — zip or dir, transparently —
    # and the canonical renderings; compare normalises both sides through the same
    # `normalise` (whose JSON form is the very pretty form explode writes) and diffs.
    files_a = {e.name: e for e in read_entries(pa)}
    files_b = {e.name: e for e in read_entries(pb)}
    names_a, names_b = set(files_a), set(files_b)

    diffs_found = False

    for name in sorted(names_a - names_b):
        print(f"only in {pa.name}: {name}")
        diffs_found = True
    for name in sorted(names_b - names_a):
        print(f"only in {pb.name}: {name}")
        diffs_found = True

    for name in sorted(names_a & names_b):
        try:
            text_a = normalise(files_a[name])
            text_b = normalise(files_b[name])
        except Exception as e:
            print(f"  could not parse {name}: {e}")
            diffs_found = True
            continue

        if text_a == text_b:
            continue

        diffs_found = True
        lines = list(difflib.unified_diff(
            text_a.splitlines(),
            text_b.splitlines(),
            fromfile=f"{pa.name}/{name}",
            tofile=f"{pb.name}/{name}",
            lineterm="",
            n=3,
        ))
        print("\n".join(lines))

    return diffs_found
