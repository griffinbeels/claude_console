"""Block/allow corpus for the PostToolUse consumer-check hook.

A guard is only worth having if it fires on the thing it is for and stays out
of the way otherwise, and both halves have to be pinned — a hook that
false-triggers gets switched off within a day, and one that never fires is
indistinguishable from not existing.

`subprocess.run` is patched in every case that reaches it. Unpatched, the hook
would run task_tracker's whole suite, which is both slow and a real side effect
for a unit test.
"""

import ast
import io
import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO / ".claude" / "hooks"))

import consumer_check  # noqa: E402

PACKAGE_FILE = str(REPO / "claude_console" / "session.py")


@pytest.fixture(autouse=True)
def never_really_runs_a_consumer(monkeypatch, tmp_path):
    """Default: the checker is green, records the call, and no escape file.

    `ESCAPE_FILE` is swapped for a path under tmp_path rather than having its
    `.exists` patched — a Path's methods are read-only on the instance
    (`AttributeError: 'WindowsPath' object attribute 'exists' is read-only`),
    which is not obvious until every test in the file errors at setup.
    """
    calls = []

    def fake_run(args, **kwargs):
        calls.append(args)
        return subprocess.CompletedProcess(args, 0, stdout="pass", stderr="")

    monkeypatch.setattr(consumer_check.subprocess, "run", fake_run)
    monkeypatch.setattr(consumer_check, "ESCAPE_FILE", tmp_path / "absent")
    return calls


def fire(payload, monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO(json.dumps(payload)))
    return consumer_check.main()


def edit_of(path):
    return {"tool_name": "Edit", "tool_input": {"file_path": path}}


# --- ALLOW: must exit 0 AND must not spend five seconds on a consumer suite ---

@pytest.mark.parametrize("payload", [
    pytest.param({"tool_name": "Bash", "tool_input": {"command": "ls"}},
                 id="not-an-edit"),
    pytest.param(edit_of(str(REPO / "tests" / "test_session.py")),
                 id="edits-a-test-not-the-package"),
    pytest.param(edit_of(str(REPO / "README.md")), id="edits-the-readme"),
    pytest.param(edit_of(str(REPO / "CLAUDE.md")), id="edits-the-notes"),
    pytest.param(edit_of(str(REPO / "consumers.json")), id="edits-the-registry"),
    pytest.param(edit_of(str(REPO / ".claude" / "hooks" / "consumer_check.py")),
                 id="edits-the-hook-itself"),
    pytest.param(edit_of(r"C:\elsewhere\another_project\launcher.py"),
                 id="edits-a-different-repo"),
    pytest.param({"tool_name": "Edit", "tool_input": {}}, id="no-file-path"),
    pytest.param({"tool_name": "Edit", "tool_input": "nonsense"},
                 id="tool-input-not-a-dict"),
    pytest.param({"tool_name": "Edit"}, id="no-tool-input"),
    pytest.param({}, id="empty-payload"),
])
def test_allowed_without_running_any_consumer(payload, monkeypatch,
                                              never_really_runs_a_consumer):
    assert fire(payload, monkeypatch) == 0
    assert never_really_runs_a_consumer == [], (
        "this payload changes nothing a consumer imports, so paying for a "
        "full consumer suite would train everyone to switch the hook off")


def test_a_payload_that_is_not_json_fails_open(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO("not json at all"))

    assert consumer_check.main() == 0


def test_a_payload_that_is_not_an_object_fails_open(monkeypatch):
    monkeypatch.setattr(sys, "stdin", io.StringIO('"a bare string"'))

    assert consumer_check.main() == 0


# --- BLOCK: a package edit that breaks a consumer ---

def test_editing_the_package_runs_the_consumer_suites(
        monkeypatch, never_really_runs_a_consumer):
    assert fire(edit_of(PACKAGE_FILE), monkeypatch) == 0
    assert len(never_really_runs_a_consumer) == 1, (
        "a change under claude_console/ is live in every consumer immediately")


@pytest.mark.parametrize("tool", ["Write", "Edit", "MultiEdit"])
def test_every_edit_tool_is_covered(tool, monkeypatch,
                                    never_really_runs_a_consumer):
    # The matcher in settings.json names all three; a hook that only understood
    # Edit would be silently half-installed.
    fire({"tool_name": tool, "tool_input": {"file_path": PACKAGE_FILE}},
         monkeypatch)

    assert len(never_really_runs_a_consumer) == 1


def test_a_failing_consumer_blocks_with_its_output(monkeypatch, capsys):
    def failing_run(args, **kwargs):
        return subprocess.CompletedProcess(
            args, 1, stdout="", stderr="--- task_tracker ---\nE   ImportError")

    monkeypatch.setattr(consumer_check.subprocess, "run", failing_run)

    assert fire(edit_of(PACKAGE_FILE), monkeypatch) == 2
    reported = capsys.readouterr().err
    # Exit 2 puts stderr in front of the model, so the detail has to be there —
    # "a consumer broke" with no name and no traceback is not actionable.
    assert "task_tracker" in reported
    assert "ImportError" in reported


def test_the_escape_file_silences_it(monkeypatch, tmp_path,
                                     never_really_runs_a_consumer):
    present = tmp_path / "skip-consumer-check"
    present.write_text("deliberate breakage, updating the consumer next\n",
                       encoding="utf-8", newline="\n")
    monkeypatch.setattr(consumer_check, "ESCAPE_FILE", present)

    assert fire(edit_of(PACKAGE_FILE), monkeypatch) == 0
    assert never_really_runs_a_consumer == []


def test_a_wedged_or_missing_checker_fails_open(monkeypatch):
    def exploding_run(args, **kwargs):
        raise subprocess.TimeoutExpired(args, 1)

    monkeypatch.setattr(consumer_check.subprocess, "run", exploding_run)

    # The check being broken is not evidence the edit is wrong, and blocking on
    # it would make this hook the problem it exists to prevent.
    assert fire(edit_of(PACKAGE_FILE), monkeypatch) == 0


def test_everything_the_guard_prints_is_ascii():
    """Windows writes stderr in the system codepage, and the reader is the model.

    Python's stderr on this machine is cp1252, so an em dash in the report came
    back as `?` in a live run of this hook. That text exists to tell the model
    what it just broke; a mangled character in it is a small thing, but the same
    encoding path has silently corrupted lint messages here before.

    Only string *literals that get printed* are checked, not docstrings or
    comments — those never reach a pipe and keep their punctuation.
    """
    offenders = []
    for source in (REPO / ".claude" / "hooks" / "consumer_check.py",
                   REPO / "tools" / "check_consumers.py"):
        tree = ast.parse(source.read_text(encoding="utf-8"))
        docstrings = {
            node.body[0].value
            for node in ast.walk(tree)
            if isinstance(node, (ast.Module, ast.FunctionDef, ast.ClassDef))
            and node.body and isinstance(node.body[0], ast.Expr)
            and isinstance(node.body[0].value, ast.Constant)
            and isinstance(node.body[0].value.value, str)
        }
        for node in ast.walk(tree):
            if (isinstance(node, ast.Constant) and isinstance(node.value, str)
                    and node not in docstrings
                    and not node.value.isascii()):
                offenders.append(f"{source.name}:{node.lineno} {node.value[:40]!r}")

    assert not offenders, (
        "non-ASCII in a string this guard may print; cp1252 mangles it on the "
        "way to the reader: " + ", ".join(offenders))


def test_the_hook_is_actually_wired_up():
    """A hook nobody registered is a file, not a guard.

    The likeliest way this whole mechanism silently stops existing is settings
    .json being rewritten without it — at which point every test above still
    passes, because they call main() directly.
    """
    settings = json.loads(
        (REPO / ".claude" / "settings.json").read_text(encoding="utf-8"))

    registered = [
        hook["args"][0]
        for entry in settings["hooks"]["PostToolUse"]
        for hook in entry["hooks"]
        if hook.get("args")
    ]

    assert any("consumer_check.py" in arg for arg in registered), (
        "consumer_check.py is not registered in .claude/settings.json")
    matchers = [entry["matcher"] for entry in settings["hooks"]["PostToolUse"]]
    assert any("Write" in m and "Edit" in m for m in matchers)
