"""neutralise (explode) and resolve (build) over entries; both return problems."""

import json
import re

import pytest

from cimkit_git import errors, placeholders, source
from cimkit_git.config import Placeholders
from cimkit_git.formats import zip_json
from cimkit_git.placeholders import Problem
from tests.test_codec_roundtrip import CORPUS, _corpus_projects

PH = Placeholders(
    fields=("workspaceConnectionString",),
    format="@@{key}@@",
    keys=("main_gdb", "archive_gdb"),
    targets=("local", "dev"),
)
# Backslashes, semicolons and quotes all need JSON escaping, so a text replace would corrupt them.
MAIN = 'DATABASE=C:\\data\\"main".gdb;AUTHENTICATION_MODE=OSA'


def entry(obj):
    return json.dumps(obj, separators=(",", ":")).encode()


def test_neutralise_replaces_a_registered_value_in_a_nested_field():
    entries = {"Map/map.json": entry({"layers": [{"dataConnection": {"workspaceConnectionString": MAIN}}]})}

    out, problems = placeholders.neutralise(entries, {MAIN: "main_gdb"}, PH)

    assert problems == []
    assert json.loads(out["Map/map.json"]) == {
        "layers": [{"dataConnection": {"workspaceConnectionString": "@@main_gdb@@"}}]
    }


def test_neutralise_reports_an_unregistered_value_and_leaves_it():
    entries = {"a.json": entry([{"workspaceConnectionString": "DATABASE=other.gdb"}] * 2)}

    out, problems = placeholders.neutralise(entries, {MAIN: "main_gdb"}, PH)

    assert problems == [Problem("unregistered", "a.json", "DATABASE=other.gdb")]
    assert out == entries


def test_neutralise_accepts_declared_placeholders_and_reports_unknown_keys():
    entries = {
        "a.json": entry([{"workspaceConnectionString": "@@main_gdb@@"}, {"workspaceConnectionString": "@@old_gdb@@"}])
    }

    out, problems = placeholders.neutralise(entries, {}, PH)

    assert problems == [Problem("unknown_key", "a.json", "old_gdb")]
    assert out == entries


def test_neutralise_keeps_the_bytes_of_entries_it_does_not_change():
    # Spaced like a hand edit, so any re-serialisation would show.
    entries = {
        "a.json": b'{ "workspaceConnectionString" : "@@main_gdb@@" }',
        "b.xml": b"<workspaceConnectionString>DATABASE=x.gdb</workspaceConnectionString>",
        "c.dat": b"\x00\x01",
    }

    out, problems = placeholders.neutralise(entries, {"DATABASE=x.gdb": "main_gdb"}, PH)

    assert (out, problems) == (entries, [])


def test_an_entry_that_fails_to_parse_is_an_error():
    with pytest.raises(errors.EntryParseError):
        placeholders.neutralise({"a.json": b'{"workspaceConnectionString":'}, {}, PH)


def test_resolve_replaces_placeholders_with_the_target_values():
    entries = {"a.json": entry({"workspaceConnectionString": "@@main_gdb@@"})}

    out, problems = placeholders.resolve(entries, {"main_gdb": MAIN}, PH)

    assert problems == []
    assert json.loads(out["a.json"]) == {"workspaceConnectionString": MAIN}


def test_resolve_reports_missing_values_unknown_keys_and_raw_values():
    entries = {
        "a.json": entry(
            [
                {"workspaceConnectionString": "@@archive_gdb@@"},
                {"workspaceConnectionString": "@@old_gdb@@"},
                {"workspaceConnectionString": "DATABASE=raw.gdb"},
            ]
        )
    }

    out, problems = placeholders.resolve(entries, {"main_gdb": MAIN}, PH)

    assert problems == [
        Problem("missing_value", "a.json", "archive_gdb"),
        Problem("unknown_key", "a.json", "old_gdb"),
        Problem("unregistered", "a.json", "DATABASE=raw.gdb"),
    ]
    assert out == entries


def test_neutralise_reports_a_mapped_key_that_config_no_longer_declares():
    entries = {"a.json": entry({"workspaceConnectionString": MAIN})}

    out, problems = placeholders.neutralise(entries, {MAIN: "old_gdb"}, PH)

    assert problems == [Problem("unknown_key", "a.json", "old_gdb")]
    assert out == entries


@pytest.mark.skipif(not CORPUS.is_dir(), reason="cimkit-corpus is not present")
@pytest.mark.parametrize("path", _corpus_projects(), ids=lambda p: str(p.relative_to(CORPUS)))
def test_corpus_project_survives_neutralise_then_resolve(path):
    try:
        original = zip_json.read(path)
    except (errors.NotAZipError, errors.Pro2ProjectError):
        pytest.skip("not a Pro 3.x project")
    _, found = placeholders.neutralise(original, {}, PH)
    value_to_key = {p.text: f"k{i}" for i, p in enumerate(found)}
    ph = Placeholders(PH.fields, PH.format, tuple(value_to_key.values()), PH.targets)

    neutral, problems = placeholders.neutralise(original, value_to_key, ph)
    assert problems == []
    # Scanned as text, so a field the JSON walk misses still counts against it.
    raw_value = re.compile(rb'"workspaceConnectionString"\s*:\s*"(?!@@k\d+@@")')
    assert not [name for name, data in neutral.items() if raw_value.search(data)]
    resolved, problems = placeholders.resolve(neutral, {k: v for v, k in value_to_key.items()}, ph)
    assert problems == []
    assert source.render(resolved) == source.render(original)
