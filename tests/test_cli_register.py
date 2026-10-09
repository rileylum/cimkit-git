"""Registering a new value from sync and explode, in throwaway repos.

A prompt has no public seam, so these tests replace sys.stdin with scripted answers and
make stdin and stdout claim to be a terminal.
"""

import io
import sys
import tomllib

import pytest

from cimkit_git import cli, source
from cimkit_git.formats import zip_json
from tests.test_cli_status import (  # noqa: F401
    ENTRIES,
    VALUE,
    configured,
    layer,
    project,
)
from tests.test_git import repo  # noqa: F401


class Terminal(io.StringIO):
    def isatty(self):
        return True


@pytest.fixture
def terminal(monkeypatch, capsys):
    def answer(*lines):
        monkeypatch.setattr(sys, "stdin", Terminal("".join(f"{line}\n" for line in lines)))
        monkeypatch.setattr(sys.stdout, "isatty", lambda: True)

    return answer


@pytest.fixture
def ignored(configured):
    (configured / ".gitignore").write_text("cimkit.local.toml\n")
    return configured


def toml(path):
    return tomllib.loads(path.read_text())


def test_explode_in_a_terminal_registers_the_suggested_key_and_explodes(ignored, terminal, capsys):
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("")

    assert cli.main(["explode"]) == 0

    assert toml(ignored / "cimkit.toml")["git"]["placeholders"]["keys"] == ["main_gdb", "x_gdb"]
    assert toml(ignored / "cimkit.local.toml") == {"local": {"x_gdb": VALUE}}
    neutral = {**ENTRIES, "map/map.json": layer("@@x_gdb@@")}
    assert source.read_dir(ignored / "map.aprx.src") == source.render(neutral)
    assert capsys.readouterr().out.endswith("map.aprx: exploded, target local\n")


def test_a_typed_key_replaces_the_suggestion_and_a_bad_name_is_asked_again(ignored, terminal, capsys):
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("Bad-Name", "sales")

    assert cli.main(["explode"]) == 0

    assert toml(ignored / "cimkit.local.toml") == {"local": {"sales": VALUE}}
    assert "A key is lowercase [a-z0-9_]." in capsys.readouterr().out


def test_skipping_leaves_config_alone_and_prints_the_manual_steps(ignored, terminal, capsys):
    zip_json.write(ENTRIES, ignored / "map.aprx")
    before = (ignored / "cimkit.toml").read_text()
    terminal("s")

    assert cli.main(["explode"]) == 1

    assert (ignored / "cimkit.toml").read_text() == before
    assert not (ignored / "cimkit.local.toml").exists()
    assert not (ignored / "map.aprx.src").exists()
    out = capsys.readouterr().out
    assert "map.aprx: refused: placeholder problems" in out
    assert out.endswith("3. Run the command again.\n")


def test_no_input_prints_the_manual_steps_even_in_a_terminal(ignored, terminal, capsys):
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("")

    assert cli.main(["explode", "--no-input"]) == 1

    assert not (ignored / "cimkit.local.toml").exists()
    assert "Key for it" not in capsys.readouterr().out


def test_sync_registers_a_new_projects_value_and_says_which_targets_still_lack_it(ignored, terminal, capsys):
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("")

    assert cli.main(["sync"]) == 0

    out = capsys.readouterr().out
    assert "Registered x_gdb in local. Set it for dev too." in out
    assert out.endswith("map.aprx: exploded, target local\n")


def test_a_declared_key_the_target_lacks_gets_the_value_without_a_second_key(ignored, terminal, capsys):
    (ignored / "cimkit.local.toml").write_text("[dev]\nmain_gdb = 'DATABASE=dev'\n")
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("main_gdb")

    assert cli.main(["explode"]) == 0

    assert toml(ignored / "cimkit.toml")["git"]["placeholders"]["keys"] == ["main_gdb"]
    assert toml(ignored / "cimkit.local.toml") == {"dev": {"main_gdb": "DATABASE=dev"}, "local": {"main_gdb": VALUE}}
    assert "Set it for" not in capsys.readouterr().out


def test_a_key_the_target_already_has_a_value_for_is_asked_again(ignored, terminal, capsys):
    (ignored / "cimkit.local.toml").write_text("[local]\nmain_gdb = 'DATABASE=other'\n")
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("main_gdb", "")

    assert cli.main(["explode"]) == 0

    assert "main_gdb already has a value in local." in capsys.readouterr().out
    assert toml(ignored / "cimkit.local.toml")["local"] == {"main_gdb": "DATABASE=other", "x_gdb": VALUE}


def test_sync_does_not_offer_to_register_a_value_already_in_source(ignored, terminal, capsys):
    (ignored / "cimkit.local.toml").write_text("[local]\nmain_gdb = 'DATABASE=other'\n")
    source.write_dir(source.render(ENTRIES), ignored / "map.aprx.src")
    terminal("")

    assert cli.main(["sync"]) == 1

    assert "Key for it" not in capsys.readouterr().out
    assert toml(ignored / "cimkit.toml")["git"]["placeholders"]["keys"] == ["main_gdb"]


def test_a_config_the_edit_cannot_handle_falls_back_to_the_manual_steps(ignored, terminal, capsys):
    path = ignored / "cimkit.toml"
    path.write_text(path.read_text().replace('["main_gdb"]', '["main_gdb"  # the main one\n]'))
    zip_json.write(ENTRIES, ignored / "map.aprx")
    terminal("")

    assert cli.main(["explode"]) == 1

    out = capsys.readouterr().out
    assert "can't add x_gdb to placeholders.keys" in out
    assert out.endswith("3. Run the command again.\n")
    assert not (ignored / "cimkit.local.toml").exists()


def test_registering_refuses_while_git_would_track_the_local_file(configured, terminal, capsys):
    zip_json.write(ENTRIES, configured / "map.aprx")
    before = (configured / "cimkit.toml").read_text()
    terminal("")

    assert cli.main(["explode"]) == 1

    out = capsys.readouterr().out
    assert "Add cimkit.local.toml to .gitignore" in out
    assert out.endswith("3. Run the command again.\n")
    assert (configured / "cimkit.toml").read_text() == before
    assert not (configured / "cimkit.local.toml").exists()
