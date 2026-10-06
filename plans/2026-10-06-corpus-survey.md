# Corpus survey

Date: 2026-10-06. A read-only scan of every `.aprx` and `.atbx` under
`cimkit-corpus/sources/`. The script is `~/dev/cimkit/survey/survey.py`, outside the
corpus and outside this repo. Per-project records are in `~/dev/cimkit/survey/out/projects.jsonl`.

## Corpus

| Kind | Count |
|---|---|
| `.aprx`, Pro 3.x (`GISProject.json`) | 137 |
| `.aprx`, Pro 2.x (`GISProject.xml`) | 207 |
| `.atbx` | 91 |
| Not a zip | 2 |

The two non-zips are not Esri projects. One is a 67-byte git-annex pointer; the other is
an ATLAS.ti XML file with an `.aprx` suffix. The codec must reject a non-zip with a typed
error.

## Findings

### 1. ElementTree breaks Esri XML (design change)

CIM XML names types in attribute values: `xsi:type='typens:CIMMap'`. ElementTree drops
any namespace declaration that no element or attribute *name* uses, so `xmlns:typens`
and `xmlns:xs` disappear, and `typens:CIMMap` is left with an unbound prefix.

- 3,868 XML entries in 344 projects are affected. That includes `CIMDocumentInfo` in
  every Pro 3.x project, so the MVP is affected too.
- Canonical XML comparison (`ET.canonicalize`) does not catch it. The old code hid it by
  registering `typens` as the 3.6.0 schema, but the corpus uses 1.3.0 to 2.9.0, so that
  rewrote the namespace URI instead.

**Implies:** XML must not be re-serialised by ElementTree.

**Follow-up test on the 2,066 XML entries in Pro 3.x projects:** `minidom` keeps
`xmlns:*` declarations, because it treats them as attributes. It lost none, and
render → build reached a fixed point. Its `toprettyxml()` still has two traps:

- 230 elements hold only whitespace, such as `<x> </x>`. Stripping whitespace on build
  would turn `" "` into `""`.
- One metadata entry has mixed content (the text `World` beside child elements).
  `toprettyxml()` adds newlines inside that text.

**Adopted:** parse with `minidom`. Add whitespace on render, and remove it on build, only
inside elements whose children are all elements. 259 metadata entries lose their CRLF
formatting between elements on the first build; no value changes.

### 2. Case collisions are common, not rare (design wording)

87 projects have them: 63 of 137 Pro 3.x projects and 24 Pro 2.x projects. Every
collision is a directory, never a file. `Map/` vs `map/` accounts for 71 of them. A
typical case is `Map/<guid>.json` beside `map/map.json`, `map/parcels.json` and so on.
No project has more than 3 collision groups.

**Implies:** keep the `~N` escape and `_names.json`. Change "accepted as rare" in the
design, because about half of real 3.x projects will carry `_names.json`. Slice 1 needs
corpus-driven tests for this, not just the fixture.

### 3. Entry kind by content: confirmed

Sniffing the first non-space byte (`{` or `[` for JSON, `<` for XML) classified every
entry. Nothing failed to parse.

| Suffix | Holds | Where |
|---|---|---|
| `.xml` | JSON | 932 entries in Pro 3.x projects |
| `.content`, `.rc`, `.model`, `.diagram` | JSON | `.atbx` |
| `.dat` | JSON (11), XML (10), opaque (577) | `.aprx` |
| `.py` | Python source, so opaque | `.atbx` |

Two odd names need handling:

- `scene/.json`, an entry with an empty stem, in two projects. It becomes a dotfile in
  Source.
- Three `.atbx` files hold directory entries such as `KvaByPhase.tool/`. Those are also
  the only files with stored (uncompressed) entries.

Name validation found nothing worse. There were no `..` segments, absolute names,
backslashes, duplicate names, or Windows-illegal names.

All entries are UTF-8 with no BOM. 6,678 XML entries use CRLF, and 3,884 start with an
`<?xml?>` declaration. Rendering will normalise both.

### 4. Pro 2.x XML projects

207 projects use the 2.x XML format, and most Learn packages ship both a 2.x and a 3.x
copy. Pro 3 converts a project to JSON on its first save.

**Ruling:** 2.x is not supported for now. Explode refuses it with "open and save in Pro 3
first". Metadata XML appears in 3.x projects too, so the XML renderer is still needed.

### 5. Absolute paths, UNC paths and URLs

All 346 `.aprx` files hit, usually many times each: 8,376 hits in total. Most of the hits
are noise for a leak check:

- **XML namespaces** inside embedded-XML string fields (`settingsXML`, `propertiesXML`,
  `viewLayoutXML`): about 4,000 `www.w3.org` and `schemas.*` hits.
- **Public Esri services**: `services.arcgisonline.com`, `geocode.arcgis.com`,
  `cdn.arcgis.com`, and others.
- **`Metadata/*.xml`**: the project's own path in `DocumentTitle` (339 projects),
  geoprocessing history in `Process@ToolSource`, and UNC paths in `linkage` and `onlink`.

Fields beyond `pathHint` that hold machine paths: `catalogPath`, style `name` paths
(`C:\...\3D Basic.stylx`), and `parameter@catalogPath` in `.atbx`. Only 7 projects have
an absolute `workspaceConnectionString`; most tutorial data is relative or a web service
URL.

2.x XML uses PascalCase field names (`WorkspaceConnectionString`, `PathHint`), while
3.x JSON uses camelCase.

**Implies:**

- The `check` warning as written fires on every project and will be ignored. **Adopted:**
  warn on local absolute paths and UNC paths only, never on URLs.
- **Ruling:** `Metadata/` is scanned. It holds real machine paths, but no connection
  strings.
- Configured fields are matched by exact key. With 2.x cut, camelCase is enough.

### 6. Determinism: holds, with two caveats

For all 435 zips, the modelled codec (sorted entries, fixed timestamp, level 6,
pretty-printed Source, minified build) behaves as follows:

- Building the same Source twice gives identical bytes.
- explode → build → explode → build reaches a fixed point after one cycle, for both the
  Source and the binary.
- No build matches the original file byte for byte, which is expected. 2,026 of 12,688
  entries are byte-identical after one cycle.

The two caveats:

- **Number text changes.** Python rewrites `0.0000005` as `5e-07`, and
  `0.18431372549019609` as `0.1843137254901961`. The value is the same double, but 2,205
  JSON entries change bytes on the first build. Pro accepts exponent notation: it writes
  numbers like `1.2728230697746809e-15` itself, in 901 entries. So no custom number
  writer is needed. The design's rule that "an in-memory explode that equals
  the recorded Source counts as unchanged" already absorbs the hash change.
- **"Same bytes on every machine" is not guaranteed.** DEFLATE output depends on the
  zlib build: zlib 1.3.2 here, Python's bundled zlib on Windows, and zlib-ng on some
  distributions. **Adopted:** compare binaries by a hash over (name, uncompressed bytes),
  and keep DEFLATE.

Pro writes the timestamp `(1980, 0, 0)` or `(1980, 1, 0)`, which are invalid DOS dates.
Pinning `(1980, 1, 1)` is fine.

### 7. Other checks: clean

- No duplicate JSON keys, so a `dict` round trip loses nothing.
- No `NaN` or `Infinity` literals.
- No zip comments or extra fields.

## Design changes made (draft 4)

1. Codec and Source: XML through `minidom` with the element-only whitespace rule
   (finding 1).
2. Codec and Source: case collisions are common; the escape stays (finding 2).
3. Codec: rejects non-zips and Pro 2.x; drops directory entries; allows empty stems
   (findings 3 and 4, rulings 8).
4. Sync state: `binary_hash` hashes the entries, not the zip bytes (finding 6).
5. Checks: warn on local absolute and UNC paths, including `Metadata/`; no URL warning
   (finding 5, ruling 9).
6. Open question 4: Pro must confirm it opens a built binary.
