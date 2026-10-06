import sys
import zipfile
from pathlib import Path

from .util import aprx_output_for
from .transform import IDENTITY
from .entry import read_entries, render_min

_DOS_EPOCH = (1980, 1, 1, 0, 0, 0)


def pack(src_dir: str, output_path: str = None, transform=IDENTITY) -> Path:
    """Pack an exploded .aprx.src directory back into an .aprx file.

    The version-control core knows nothing about connections (ADR-0002): it parses
    each JSON entry, hands it to ``transform.apply`` (which may rewrite it in place),
    and minifies the result. Simple mode passes the no-op ``IDENTITY`` (a faithful,
    byte-stable rebuild); environment mode passes a ``Substitution`` that swaps
    ``@@token@@`` placeholders for one environment's real connection strings. The
    composition root (CLI / hooks) chooses which, so pack itself never imports the
    connection engine.

    The transform is two-phase: ``apply`` runs per entry, then ``raise_if_problems``
    fires once after every entry is computed but **before** the archive is opened — so
    a bad entry (e.g. a token with no value in the chosen environment) aborts the whole
    pack without writing a Project full of unsubstituted tokens, and every offender is
    reported at once. Returns the output file path.
    """
    src = Path(src_dir)
    if not src.is_dir():
        sys.exit(f"cimkit-git: {src} is not a directory")

    if output_path:
        out = Path(output_path)
    else:
        out = aprx_output_for(src)

    # Compute every entry first so a transform problem fails before we open the archive.
    # The Entry reader (issue 0002) owns the directory walk (in sorted order — what makes
    # the archive below byte-stable), the JSON parse, and the byte-fallback policy; pack
    # hands each parsed entry to the transform and renders the minified form. The
    # deterministic *write* (DOS-epoch timestamp, compresslevel) stays here, a pack concern.
    payloads = []  # (arcname, data: bytes)
    for entry in read_entries(src):
        if entry.is_parsed_json:
            transform.apply(entry.parsed)
        payloads.append((entry.name, render_min(entry)))

    # Phase 2: surface every accumulated problem at once, before touching the archive.
    transform.raise_if_problems()

    with zipfile.ZipFile(out, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=6) as zf:
        for arcname, data in payloads:
            info = zipfile.ZipInfo(arcname, date_time=_DOS_EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            zf.writestr(info, data)

    size_kb = out.stat().st_size // 1024
    print(f"  packed {src.name}/ → {out.name} ({size_kb} KB, {len(payloads)} files)")
    return out
