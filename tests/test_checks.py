"""check over one project's Source files: the findings it reports."""

from cimkit_git import checks, source
from cimkit_git.checks import Finding
from tests.test_cli_status import ENTRIES, NEUTRAL, VALUE, layer
from tests.test_placeholders import PH


def test_a_real_value_in_a_configured_field_is_an_error():
    files = source.render(ENTRIES)

    assert checks.check(files, PH) == [Finding("unregistered", "map/map.json", VALUE)]


def test_a_placeholder_naming_an_undeclared_key_is_an_error():
    files = source.render({**ENTRIES, "map/map.json": layer("@@old_gdb@@")})

    assert checks.check(files, PH) == [Finding("unknown_key", "map/map.json", "old_gdb")]


def test_declared_placeholders_and_no_config_give_no_findings():
    assert checks.check(source.render(NEUTRAL), PH) == []
    assert checks.check(source.render(NEUTRAL), None) == []


def test_every_entry_that_fails_to_parse_is_an_error_and_the_rest_are_still_scanned():
    files = {**source.render(ENTRIES), "a.json": b'{"x":', "b.xml": b"<a><b></a>"}

    found = checks.check(files, PH)

    assert [(f.kind, f.entry) for f in found] == [("parse", "a.json"), ("parse", "b.xml"), ("unregistered", "map/map.json")]


def test_machine_paths_give_one_warning_per_entry_with_the_first_path_and_a_count():
    files = {
        "a.json": b'{"pathHint": "C:\\\\Users\\\\x\\\\map.aprx", "styles": ["D:/styles/3D Basic.stylx"]}',
        "Metadata/m.xml": b'<m><linkage href="\\\\server\\share\\a.gdb"/><t>c:\\x</t></m>',
        "urls.json": b'{"u": "https://services.arcgisonline.com/x", "n": "http://www.w3.org/2001/XMLSchema"}',
    }

    assert checks.check(files, None) == [
        Finding("path", "a.json", r"C:\Users\x\map.aprx", 2),
        Finding("path", "Metadata/m.xml", r"\\server\share\a.gdb", 2),
    ]
