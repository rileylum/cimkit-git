import json
import stat
import subprocess
import sys
from pathlib import Path

import pytest

from cimkit_git.connections import CONFIG_FILENAME
from cimkit_git.install import _read_config, install, install_hooks, MARKER
from cimkit_git.util import git_root


def _config(repo: Path) -> dict:
    return json.loads((repo / CONFIG_FILENAME).read_text())


def _no_prompt(*_a, **_k):
    raise AssertionError("install must not prompt in this scenario")


@pytest.fixture
def git_repo(tmp_path):
    subprocess.run(["git", "init", str(tmp_path)], check=True, capture_output=True)
    return tmp_path


ALL_HOOKS = ("pre-commit", "pre-push", "post-stash", "post-merge", "post-checkout")


def test_hooks_are_written(git_repo):
    install_hooks(git_repo)
    for name in ALL_HOOKS:
        assert (git_repo / ".git" / "hooks" / name).exists()


def test_hooks_are_executable(git_repo):
    install_hooks(git_repo)
    for name in ALL_HOOKS:
        hook = git_repo / ".git" / "hooks" / name
        assert hook.stat().st_mode & stat.S_IXUSR


def test_hooks_contain_marker(git_repo):
    install_hooks(git_repo)
    for name in ALL_HOOKS:
        hook = git_repo / ".git" / "hooks" / name
        assert MARKER in hook.read_text()


def test_pre_push_runs_verify_without_install_hint(git_repo):
    # pre-push must let `git cim verify` speak for itself, not mask a real failure
    # with the generic "is cimkit-git installed?" message.
    install_hooks(git_repo)
    text = (git_repo / ".git" / "hooks" / "pre-push").read_text()
    assert "hook pre-push" in text
    assert "is cimkit-git installed" not in text


def test_install_is_idempotent(git_repo):
    install_hooks(git_repo)
    install_hooks(git_repo)
    for name in ALL_HOOKS:
        assert MARKER in (git_repo / ".git" / "hooks" / name).read_text()


def test_does_not_overwrite_foreign_hook(git_repo, capsys):
    foreign = "#!/usr/bin/env bash\necho 'foreign'\n"
    hook_path = git_repo / ".git" / "hooks" / "pre-commit"
    hook_path.write_text(foreign)
    install_hooks(git_repo)
    assert hook_path.read_text() == foreign
    assert "pre-commit" in capsys.readouterr().out


def test_fails_outside_git_repo(tmp_path):
    with pytest.raises(SystemExit):
        git_root(tmp_path)


# --------------------------------------------------------------------------- #
# Mode opt-in (issue 0006) — install records the Project's Mode in aprx.json.
# --------------------------------------------------------------------------- #

def test_interactive_run_prompts_and_writes_mode(git_repo):
    # AC1: first interactive run prompts for Mode and writes the choice.
    install(repo_root=git_repo, config_dir=git_repo,
            interactive=True, prompt=lambda _msg: "env")
    assert _config(git_repo)["mode"] == "env"


def test_mode_flag_writes_without_prompting(git_repo):
    # AC2: --mode bypasses the prompt entirely.
    for mode in ("simple", "env"):
        repo = git_repo / mode
        repo.mkdir()
        subprocess.run(["git", "init", str(repo)], check=True, capture_output=True)
        install(repo_root=repo, config_dir=repo, mode=mode, prompt=_no_prompt)
        assert _config(repo)["mode"] == mode


def test_non_tty_no_config_defaults_to_simple_with_warning(git_repo, capsys):
    # AC3: non-TTY, no flag, no config → simple + a warning naming env mode.
    install(repo_root=git_repo, config_dir=git_repo, interactive=False)
    assert _config(git_repo)["mode"] == "simple"
    err = capsys.readouterr().err.lower()
    assert "environment mode" in err
    assert "--mode env" in err


def test_existing_config_is_honoured_not_overwritten(git_repo, capsys):
    # AC4: an existing declaration is honoured without a prompt and left intact.
    (git_repo / CONFIG_FILENAME).write_text(
        json.dumps({"mode": "env", "token": "@@{key}@@", "fields": ["ws"]})
    )
    install(repo_root=git_repo, config_dir=git_repo, prompt=_no_prompt)
    cfg = _config(git_repo)
    assert cfg["mode"] == "env"          # unchanged
    assert cfg["token"] == "@@{key}@@"   # other fields preserved
    assert cfg["fields"] == ["ws"]
    assert "env" in capsys.readouterr().out


def test_mode_flag_overrides_blank_config_preserving_fields(git_repo):
    # A pre-existing aprx.json with no `mode` (e.g. from `connections init`) gets
    # the decided mode merged in without losing its fields/token.
    (git_repo / CONFIG_FILENAME).write_text(
        json.dumps({"fields": ["ws"], "token": "T-{key}"})
    )
    install(repo_root=git_repo, config_dir=git_repo, mode="env", prompt=_no_prompt)
    cfg = _config(git_repo)
    assert cfg["mode"] == "env"
    assert cfg["fields"] == ["ws"]
    assert cfg["token"] == "T-{key}"


def test_install_also_installs_hooks(git_repo):
    install(repo_root=git_repo, config_dir=git_repo, mode="simple", prompt=_no_prompt)
    for name in ALL_HOOKS:
        assert (git_repo / ".git" / "hooks" / name).exists()


def test_prompt_reprompts_until_valid(git_repo):
    answers = iter(["maybe", "", "env"])
    install(repo_root=git_repo, config_dir=git_repo,
            interactive=True, prompt=lambda _msg: next(answers))
    assert _config(git_repo)["mode"] == "env"


def test_mode_flag_matching_existing_is_honoured(git_repo):
    (git_repo / CONFIG_FILENAME).write_text(json.dumps({"mode": "env"}))
    install(git_repo, config_dir=git_repo, mode="env", prompt=_no_prompt)
    assert _config(git_repo)["mode"] == "env"


def test_conflicting_mode_flag_refuses_to_overwrite(git_repo):
    (git_repo / CONFIG_FILENAME).write_text(json.dumps({"mode": "env"}))
    with pytest.raises(SystemExit):
        install(git_repo, config_dir=git_repo, mode="simple", prompt=_no_prompt)
    assert _config(git_repo)["mode"] == "env"  # untouched


def test_invalid_mode_is_rejected(git_repo):
    with pytest.raises(SystemExit):
        install(git_repo, config_dir=git_repo, mode="environment")
    assert not (git_repo / CONFIG_FILENAME).exists()


def test_eof_at_prompt_exits_cleanly(git_repo):
    def _eof(_msg):
        raise EOFError
    with pytest.raises(SystemExit):
        install(git_repo, config_dir=git_repo, interactive=True, prompt=_eof)


def test_outside_git_repo_writes_no_config(tmp_path):
    # Resolve-repo-first: a non-repo run must not leave an orphan aprx.json.
    with pytest.raises(SystemExit):
        install(config_dir=tmp_path, mode="env")
    assert not (tmp_path / CONFIG_FILENAME).exists()


def test_read_config_missing_is_fresh_install(git_repo):
    # The one case that is *not* an error: an absent aprx.json is the fresh-install
    # signal, (None, {}) — decide a mode and write a new config. A present-but-broken
    # file (below) must NOT share this return, or install would clobber it.
    assert _read_config(git_repo / CONFIG_FILENAME) == (None, {})


def test_read_config_aborts_on_non_utf8(git_repo):
    # A present non-UTF-8 aprx.json (UTF-16/Latin-1, a stray 0xFF byte) is reported, not
    # swallowed into (None, {}): swallowing would let install overwrite a committed config
    # it merely failed to decode, dropping the developer's fields/token. read_json_or_exit
    # gives the distinct encoding diagnostic; install is a one-shot, so aborting is safe.
    cfg_path = git_repo / CONFIG_FILENAME
    cfg_path.write_bytes(b"\xff\xfe{ not utf-8 ")
    with pytest.raises(SystemExit) as exc:
        _read_config(cfg_path)
    assert "UTF-8" in str(exc.value)


def test_read_config_aborts_on_unreadable(git_repo, deny_reading):
    # Present but unreadable (permissions / transient I/O). Denied via monkeypatch so it
    # holds even as root. The nastiest swallow case: a transient error on a *healthy*
    # file would otherwise clobber it down to a bare mode — so it must report instead.
    cfg_path = git_repo / CONFIG_FILENAME
    cfg_path.write_text(json.dumps({"mode": "env"}), encoding="utf-8")
    deny_reading(cfg_path)
    with pytest.raises(SystemExit) as exc:
        _read_config(cfg_path)
    msg = str(exc.value)
    assert "could not be read" in msg
    assert "permission" in msg.lower()


def test_read_config_aborts_on_non_object(git_repo):
    # Valid JSON that is not an object ([], null, a bare string) can't carry a mode and
    # would crash cfg.get("mode"); reported with a directed shape message instead.
    (git_repo / CONFIG_FILENAME).write_text("[]", encoding="utf-8")
    with pytest.raises(SystemExit) as exc:
        _read_config(git_repo / CONFIG_FILENAME)
    assert "JSON object" in str(exc.value)


def test_install_against_broken_config_aborts_without_clobbering(git_repo):
    # The core fix: install against a Project whose committed aprx.json is broken
    # (here non-UTF-8, carrying recoverable fields/token) must abort with a directed
    # message and leave the file byte-for-byte intact — never silently overwrite it
    # with a bare {"mode": ...}, which would destroy the fields/token a `git checkout`
    # could otherwise still recover.
    cfg_path = git_repo / CONFIG_FILENAME
    original = b"\xff\xfe" + json.dumps(
        {"mode": "env", "fields": ["ws"], "token": "T-{key}"}
    ).encode("utf-8")
    cfg_path.write_bytes(original)
    with pytest.raises(SystemExit) as exc:
        install(git_repo, config_dir=git_repo, mode="env", prompt=_no_prompt)
    assert "UTF-8" in str(exc.value)
    assert cfg_path.read_bytes() == original   # left untouched, not clobbered


def test_main_install_mode_flag(git_repo, monkeypatch):
    # AC5: exercised through the real CLI entry point.
    monkeypatch.chdir(git_repo)
    monkeypatch.setattr(sys, "argv", ["aprx", "install", "--mode", "env"])
    from cimkit_git.__main__ import main
    main()
    assert _config(git_repo)["mode"] == "env"
