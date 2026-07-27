"""Spawning a session and finding the process to type into.

Nothing here spawns a real process or touches a real console. The one test in
this repo that opens a genuine console lives in test_console_input.py, and the
console it opens is windowless — see tests/test_conventions.py for why that is
enforced rather than trusted.
"""

import subprocess
from pathlib import Path

import pytest

import claude_console
from claude_console import console_input, session


class FakeSession:
    """Stands in for the Popen of the session itself.

    There is no console host in between any more, so this pid is the one
    everything attaches to — see `session.session_pid`.
    """
    pid = 4242


@pytest.fixture
def spawned(monkeypatch):
    """Swallow the process spawn and record what would have been sent to it."""
    typed = {}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(
        console_input, "deliver_when_ready",
        lambda pid, commands, text, on_finish=None: typed.update(
            pid=pid, commands=commands, text=text))
    return typed


def test_spawn_uses_a_new_console_in_the_given_directory(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    session.spawn_claude(Path("C:/repos/a_consumer"))

    assert captured["args"] == session.DEFAULT_LAUNCH
    assert captured["kwargs"]["cwd"] == Path("C:/repos/a_consumer")
    assert captured["kwargs"]["creationflags"] == session.NEW_CONSOLE


def test_no_console_host_is_named_so_the_machine_default_draws_the_window():
    """The window is Windows Terminal's because that is what this machine uses.

    `conhost.exe` used to be prepended here to opt out of the default-terminal
    delegation, which cost the session every glyph conhost cannot draw — `⎿`,
    on every tool result line, has no monospaced font on this machine that
    covers it (measured 2026-07-26 over every installed font). Naming no host
    hands the window back to the user's own setting, which is the one he
    develops in.
    """
    assert not hasattr(session, "CONSOLE_HOST")
    assert "conhost.exe" not in session.DEFAULT_LAUNCH


def test_the_default_launch_runs_claude_inside_powershell():
    # PowerShell is what this machine develops in, and -NoExit leaves its
    # prompt in the session's directory when Claude exits rather than taking
    # the window and its scrollback away.
    assert session.DEFAULT_LAUNCH[0] == "powershell.exe"
    assert "-NoExit" in session.DEFAULT_LAUNCH
    assert session.DEFAULT_LAUNCH[-1] == "claude --dangerously-skip-permissions"


def test_the_session_window_is_allowed_to_come_to_the_front(monkeypatch):
    """Opening a session is a human gesture, and one earns the focus it takes.

    This asserts the *absence* of the startupinfo that used to be passed. The
    rule it enforced was over-broad: focus is opt-in, and a button press opts
    in. What must still open nothing is a TEST, and that is enforced in
    tests/test_conventions.py rather than here.
    """
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kwargs: captured.update(kwargs))

    session.spawn_claude(Path("C:/repos/x"))

    assert captured.get("startupinfo") is None


def test_a_helper_the_tool_spawns_for_itself_still_gets_no_focus():
    """The other half of the split, and the reason this helper outlived its use.

    Nothing in this module passes `unfocused_startup` any more, but
    task_tracker's `restart.py` relaunches the tracker through it. A window the
    user did not ask for still may not activate.
    """
    startup = session.unfocused_startup()

    assert startup.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == session.SW_SHOWNOACTIVATE


def test_spawn_honours_a_launch_override(monkeypatch):
    # The override replaces the whole argv, PowerShell wrapper included — a
    # caller that names its own shell gets exactly that and nothing prepended.
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kwargs: captured.update(args=args))

    session.spawn_claude(Path("C:/repos/x"), launch=["pwsh", "-c", "claude"])

    assert captured["args"] == ["pwsh", "-c", "claude"]


def test_spawn_skips_permission_prompts_by_default(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kwargs: captured.update(args=args))

    session.spawn_claude(Path("C:/repos/x"))

    assert "--dangerously-skip-permissions" in " ".join(captured["args"])


def test_the_typist_is_given_the_spawned_process_itself():
    """With no host in between, the Popen IS the session.

    Walking to a child was right only while conhost sat in the middle —
    `AttachConsole` refuses a console host's own pid. Measured 2026-07-26
    without one: the first child was an incidental helper the shell had
    started, so the old walk returned a pid that could not be typed into.

    The PowerShell wrapper does not reintroduce the problem. `powershell.exe`
    shares the console it was given, so attaching to it reaches the same screen
    buffer `claude` is painting.
    """
    assert session.session_pid(FakeSession()) == FakeSession.pid


def test_open_session_reports_the_process_to_type_into(spawned):
    opened = claude_console.open_session(Path("C:/repos/x"))

    assert opened.pid == FakeSession.pid
    assert opened.host is not None


def test_a_session_that_cannot_start_raises_rather_than_failing_quietly(monkeypatch):
    # Everything AFTER the process exists gives up quietly. The spawn itself
    # must not: a missing `claude` on PATH is the caller's to report.
    def exploding_popen(args, **kwargs):
        raise FileNotFoundError("claude is not on PATH")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)

    with pytest.raises(FileNotFoundError):
        claude_console.open_session(Path("C:/repos/x"))


def test_deliver_types_into_the_session(spawned):
    opened = claude_console.open_session(Path("C:/repos/x"))

    opened.deliver(prompt="BUG: body", commands=["/color red"])

    assert spawned == {"pid": FakeSession.pid, "commands": ["/color red"],
                       "text": "BUG: body"}


def test_deliver_needs_neither_a_prompt_nor_commands(spawned):
    # Opening a bare session is a legitimate use, which is why this is not an
    # early return.
    opened = claude_console.open_session(Path("C:/repos/x"))

    opened.deliver()

    assert spawned == {"pid": FakeSession.pid, "commands": [], "text": ""}


def test_deliver_now_runs_on_this_thread_for_a_caller_about_to_exit(monkeypatch):
    # The CLI needs this: a daemon thread dies with the process that started
    # it, so the background form would type nothing at all from `-m`.
    delivered = {}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(console_input, "deliver",
                        lambda pid, commands, text: delivered.update(
                            pid=pid, commands=commands, text=text))

    claude_console.open_session(Path("C:/repos/x")).deliver_now(prompt="hi")

    assert delivered == {"pid": FakeSession.pid, "commands": [], "text": "hi"}


def test_a_name_rides_on_the_launch_instead_of_being_typed():
    """The flag exists, and it is why `/rename` is off the critical path.

    Measured against a live session (2026-07-26): `claude -n 'NAME'` puts the
    name in the prompt box's separator and in the terminal title, the moment
    the window is drawn. A typed `/rename` had to wait for a prompt box, land
    in it, and be seen to leave again before anything else could be written —
    two screen round-trips, every one of which could time out on a slow start
    and take the prompt down with it.
    """
    argv = session.default_launch("BUG: eaten prompts")

    assert argv[:-1] == session.DEFAULT_LAUNCH[:-1]
    assert argv[-1] == ("claude --dangerously-skip-permissions "
                        "-n 'BUG: eaten prompts'")


def test_an_apostrophe_in_a_name_cannot_break_out_of_its_quotes():
    # A session name is a task title — user text, going into a PowerShell
    # command line. Inside single quotes only `'` is special, and doubling it
    # is how PowerShell escapes it.
    assert session.powershell_quote("Friday's tasks") == "'Friday''s tasks'"
    assert session.default_launch("Friday's tasks")[-1].endswith(
        "-n 'Friday''s tasks'")


def test_a_newline_in_a_name_cannot_become_a_second_command():
    # `safe_line` runs inside default_launch rather than being the caller's
    # job, so no consumer can skip it. A raw newline in a -Command string is a
    # statement separator.
    argv = session.default_launch("tasks\nrm -rf /")

    assert "\n" not in argv[-1]
    assert argv[-1].endswith("-n 'tasks rm -rf /'")


def test_a_name_with_nothing_left_after_cleaning_is_no_name_at_all():
    # Not `-n ''`, which would name the session the empty string.
    assert session.default_launch("") == session.DEFAULT_LAUNCH
    assert session.default_launch("\x1b\x00\x7f") == session.DEFAULT_LAUNCH


def test_a_long_name_is_capped_before_it_reaches_the_command_line():
    argv = session.default_launch("x" * 200)

    assert argv[-1].endswith("-n '" + "x" * 59 + "…'")


def test_open_session_puts_the_name_on_the_launch_and_types_no_rename(monkeypatch):
    captured = {}
    typed = {}

    def fake_popen(args, **kwargs):
        captured["argv"] = args
        return FakeSession()

    monkeypatch.setattr(subprocess, "Popen", fake_popen)
    monkeypatch.setattr(
        console_input, "deliver_when_ready",
        lambda pid, commands, text, on_finish=None: typed.update(commands=commands))

    opened = claude_console.open_session(Path("C:/repos/x"), name="GAP0")
    opened.deliver(prompt="BUG: body", commands=["/color green"])

    assert captured["argv"][-1].endswith("-n 'GAP0'")
    assert opened.pending_name == ""
    # The whole point: nothing about the name is typed into the session.
    assert typed["commands"] == ["/color green"]


def test_a_caller_that_brought_its_own_argv_gets_the_typed_rename_instead(monkeypatch):
    """There is no safe way to inject a flag into someone else's command line.

    So that session keeps the old behaviour rather than losing its name, and
    the fallback lives here rather than in the consumer — a consumer that has
    to choose between two ways of naming a session is a consumer with two
    behaviours to keep in step.
    """
    typed = {}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeSession())
    monkeypatch.setattr(
        console_input, "deliver_when_ready",
        lambda pid, commands, text, on_finish=None: typed.update(commands=commands))

    opened = claude_console.open_session(
        Path("C:/repos/x"), launch=["pwsh", "-c", "claude"], name="GAP0")
    opened.deliver(prompt="BUG: body", commands=["/color green"])

    assert opened.pending_name == "GAP0"
    assert typed["commands"] == ["/rename GAP0", "/color green"]
