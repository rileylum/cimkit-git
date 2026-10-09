import sys
import zipfile
from xml.dom import minidom

import pytest

from cimkit_git import errors, source
from cimkit_git.formats import zip_json

PNG = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"


def test_opaque_entries_render_and_parse_back_byte_for_byte():
    entries = {"Thumbnail.dat": PNG}

    files = source.render(entries)

    assert files == entries
    assert source.parse(files) == entries


def test_json_renders_pretty_with_unicode_kept_and_a_trailing_newline():
    files = source.render({"GISProject.json": '{"name":"Ōtautahi","extent":[1,2]}'.encode()})

    assert files["GISProject.json"].decode() == (
        '{\n  "name": "Ōtautahi",\n  "extent": [\n    1,\n    2\n  ]\n}\n'
    )


def test_json_is_detected_by_content_not_suffix():
    files = source.render({"Map/layer.xml": b'  {"a":1}'})

    assert files["Map/layer.xml"] == b'{\n  "a": 1\n}\n'


def test_json_parses_back_minified():
    files = {"GISProject.json": '{\n  "name": "Ōtautahi",\n  "n": [\n    1\n  ]\n}\n'.encode()}

    assert source.parse(files) == {"GISProject.json": '{"name":"Ōtautahi","n":[1]}'.encode()}


def test_render_rejects_an_entry_that_looks_like_json_but_does_not_parse():
    with pytest.raises(errors.EntryParseError, match="Map/broken.json"):
        source.render({"Map/broken.json": b'{"a": '})


def test_parse_rejects_a_source_file_that_looks_like_json_but_does_not_parse():
    with pytest.raises(errors.EntryParseError, match="Map/broken.json"):
        source.parse({"Map/broken.json": b'{"a": '})


DECL = '<?xml version="1.0" encoding="utf-8"?>'
NS = 'xmlns:typens="http://www.esri.com/schemas/ArcGIS/2.9.0" xmlns:xsi="http://www.w3.org/2001/XMLSchema-instance"'
# As Pro writes it: single-quoted attributes, CRLF, no whitespace between elements.
PRO_XML = (
    "<?xml version='1.0' encoding='utf-8'?>\r\n"
    f"<Doc {NS} xsi:type='typens:CIMDocumentInfo'>"
    "<Title>A  b</Title><Blank> </Blank><Items><Item>1</Item></Items></Doc>"
)
SOURCE_XML = (
    f"{DECL}\n"
    f'<Doc {NS} xsi:type="typens:CIMDocumentInfo">\n'
    "  <Title>A  b</Title>\n"
    "  <Blank> </Blank>\n"
    "  <Items>\n"
    "    <Item>1</Item>\n"
    "  </Items>\n"
    "</Doc>\n"
)
BUILT_XML = (
    f"{DECL}"
    f'<Doc {NS} xsi:type="typens:CIMDocumentInfo">'
    "<Title>A  b</Title><Blank> </Blank><Items><Item>1</Item></Items></Doc>"
)


def test_xml_renders_indented_only_between_elements():
    files = source.render({"Metadata/doc.xml": PRO_XML.encode()})

    assert files["Metadata/doc.xml"].decode() == SOURCE_XML


def test_xml_parses_back_without_the_indentation():
    assert source.parse({"Metadata/doc.xml": SOURCE_XML.encode()}) == {"Metadata/doc.xml": BUILT_XML.encode()}


@pytest.mark.parametrize(
    "inner",
    [
        "<p>Hello <b>World</b>\r\n  </p>",  # the corpus's one mixed-content shape
        "<p> <b>x</b></p>",  # a no-break space is a value, not formatting
    ],
)
def test_xml_mixed_content_passes_through_unchanged(inner):
    entry = f"<doc>{inner}</doc>".encode()

    rendered = source.render({"m.xml": entry})["m.xml"].decode()

    assert rendered == f"{DECL}\n<doc>\n  {inner.replace(chr(13), '')}\n</doc>\n"
    assert source.parse({"m.xml": rendered.encode()})["m.xml"].decode() == f"{DECL}<doc>{inner.replace(chr(13), '')}</doc>"


# Known limit, accepted: minidom writes these characters raw, and the next parse
# normalises them. No Pro 3.x entry in the corpus uses them. The fix is a small
# serializer that writes &#9; &#10; &#13; itself. docs/design.md records the ruling.
@pytest.mark.parametrize(
    ("entry", "read_value"),
    [
        pytest.param(
            b'<a x="1&#10;2&#9;3"/>',
            lambda doc: doc.documentElement.getAttribute("x"),
            id="newline-and-tab-in-attribute",
            marks=pytest.mark.xfail(sys.version_info < (3, 13), reason="minidom escapes these from 3.13", strict=True),
        ),
        pytest.param(
            b"<a>t&#13;u</a>",
            lambda doc: doc.documentElement.firstChild.data,
            id="carriage-return-in-text",
            marks=pytest.mark.xfail(reason="minidom never escapes CR in text", strict=True),
        ),
    ],
)
def test_xml_control_characters_survive_render_and_build(entry, read_value):
    built = source.parse(source.render({"m.xml": entry}))["m.xml"]

    assert read_value(minidom.parseString(built)) == read_value(minidom.parseString(entry))


def test_xml_that_does_not_parse_is_an_error():
    with pytest.raises(errors.EntryParseError, match="Metadata/broken.xml"):
        source.render({"Metadata/broken.xml": b"<a><b></a>"})


@pytest.mark.parametrize(
    "name",
    [
        "/etc/x.json",
        "../x.json",
        "Map/../../x.json",
        "./x.json",
        "Map//x.json",
        "C:/x.json",
        "C:x.json",
        "//server/share/x.json",
        "Map\\x.json",
        "CON.json",
        "Map/lpt1",
        "Map/a?.json",
        "Map/trailing.",
        "Map/x\x01.json",
        "_names.json",
    ],
)
def test_render_rejects_names_that_are_not_safe_relative_paths_on_every_os(name):
    with pytest.raises(errors.BadEntryNameError):
        source.render({name: PNG})


def test_an_entry_with_an_empty_stem_becomes_a_dotfile():
    assert list(source.render({"scene/.json": b"{}"})) == ["scene/.json"]


def test_case_colliding_directories_are_escaped_and_restored():
    # Pro writes Map/<guid>.json beside map/map.json; "Map" sorts before "map".
    entries = {"Map/a.dat": PNG, "map/b.dat": PNG}

    files = source.render(entries)

    assert files == {"Map/a.dat": PNG, "map~2/b.dat": PNG, "_names.json": b'{\n  "map~2": "map"\n}\n'}
    assert source.parse(files) == entries


def test_a_project_without_collisions_has_no_names_file():
    assert "_names.json" not in source.render({"Map/a.dat": PNG, "Maps/b.dat": PNG})


@pytest.mark.parametrize(
    ("entries", "escaped"),
    [
        # "MAP" < "Map" < "map": each later one takes the next free number.
        (["MAP/a.dat", "Map/b.dat", "map/c.dat"], ["MAP/a.dat", "Map~2/b.dat", "map~3/c.dat"]),
        # A collision inside an escaped directory escapes both levels.
        (["Map/X/a.dat", "map/X/b.dat", "map/x/c.dat"], ["Map/X/a.dat", "map~2/X/b.dat", "map~2/x~2/c.dat"]),
        # A real map~2 keeps its name, so the escape skips to ~3.
        (["Map/a.dat", "map/b.dat", "map~2/c.dat"], ["Map/a.dat", "map~2/c.dat", "map~3/b.dat"]),
        # A case variant of the names file can't shadow it.
        (["_NAMES.json/a.dat", "Map/a.dat", "map/b.dat"], ["Map/a.dat", "_NAMES.json~2/a.dat", "map~2/b.dat"]),
    ],
)
def test_case_collisions_escape_to_unique_names_and_restore(entries, escaped):
    entries = dict.fromkeys(entries, PNG)

    files = source.render(entries)

    assert sorted(set(files) - {"_names.json"}) == escaped
    assert source.parse(files) == entries


def test_parse_rejects_a_names_file_that_restores_an_unsafe_name():
    files = {"a~2/x.dat": PNG, "_names.json": b'{"a~2": "../evil"}'}

    with pytest.raises(errors.BadEntryNameError):
        source.parse(files)


def test_parse_rejects_a_names_file_that_restores_two_files_to_one_name():
    files = {"a/x.dat": PNG, "a~2/x.dat": PNG, "_names.json": b'{"a~2": "a"}'}

    with pytest.raises(errors.DuplicateEntryError, match="a/x.dat"):
        source.parse(files)


def test_parse_rejects_a_names_file_that_is_not_valid_json():
    with pytest.raises(errors.EntryParseError, match="_names.json"):
        source.parse({"a~2/x.dat": PNG, "_names.json": b'{"a~2": '})


def test_entries_hash_ignores_entry_order():
    assert source.entries_hash({"a": b"1", "b": b"2"}) == source.entries_hash({"b": b"2", "a": b"1"})


@pytest.mark.parametrize(
    "other",
    [
        {"ab": b"c"},  # the boundary between name and data moved
        {"a": b"bd"},
        {"A": b"bc"},
        {"a": b"bc", "b": b""},
    ],
)
def test_entries_hash_differs_when_any_name_or_byte_differs(other):
    assert source.entries_hash({"a": b"bc"}) != source.entries_hash(other)


def test_entries_hash_ignores_how_the_zip_was_compressed(tmp_path):
    stored, deflated = tmp_path / "stored.aprx", tmp_path / "deflated.aprx"
    for path, method in ((stored, zipfile.ZIP_STORED), (deflated, zipfile.ZIP_DEFLATED)):
        with zipfile.ZipFile(path, "w", method) as zf:
            zf.writestr("GISProject.json", b'{"a":1}' * 50)

    assert stored.read_bytes() != deflated.read_bytes()
    assert source.entries_hash(zip_json.read(stored)) == source.entries_hash(zip_json.read(deflated))


def test_source_files_written_to_a_dir_read_back_the_same(tmp_path):
    files = {"GISProject.json": b"{}\n", "scene/.json": b"{}\n", "Map/sub/a.dat": PNG}

    source.write_dir(files, tmp_path / "p.aprx.src")

    assert source.read_dir(tmp_path / "p.aprx.src") == dict(sorted(files.items()))


def test_write_dir_refuses_an_existing_dir_so_no_stale_file_survives(tmp_path):
    with pytest.raises(FileExistsError):
        source.write_dir({"a.json": b"{}"}, tmp_path)


@pytest.mark.parametrize("target", ["outside.json", "outside_dir"])
def test_read_dir_rejects_symlinks(tmp_path, target):
    (tmp_path / "outside.json").write_bytes(b"{}")
    (tmp_path / "outside_dir").mkdir()
    src = tmp_path / "p.aprx.src"
    src.mkdir()
    (src / "link").symlink_to(tmp_path / target)

    with pytest.raises(errors.SymlinkInSourceError, match="link"):
        source.read_dir(src)
