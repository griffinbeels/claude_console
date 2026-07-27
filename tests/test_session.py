"""Spawning a session, finding it, and keeping it off the keyboard.

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


class FakeHost:
    """Stands in for the Popen of the console host a session is spawned in."""
    pid = 4242


# The session inside that host — conhost's child, and the pid anything typing
# into the window has to attach to.
CLIENT_PID = 9999

CONSOLE_WINDOW = 55


@pytest.fixture
def spawned(monkeypatch):
    """Swallow the process spawn and record what would have been sent to it."""
    typed = {}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeHost())
    monkeypatch.setattr(session, "_children_of", lambda pid: [CLIENT_PID])
    # Left running, this attaches to a console — and attaching means leaving
    # pytest's own. The tests that want it put their own recorder here.
    monkeypatch.setattr(session, "hold_focus_in_background",
                        lambda previous, pid: None)
    monkeypatch.setattr(
        console_input, "deliver_when_ready",
        lambda pid, commands, text: typed.update(
            pid=pid, commands=commands, text=text))
    return typed


def test_spawn_uses_a_new_console_in_the_given_directory(monkeypatch):
    captured = {}

    def fake_popen(args, **kwargs):
        captured["args"] = args
        captured["kwargs"] = kwargs

    monkeypatch.setattr(subprocess, "Popen", fake_popen)

    session.spawn_claude(Path("C:/repos/sm64_tracker"))

    assert captured["args"] == [session.CONSOLE_HOST] + session.DEFAULT_LAUNCH
    assert captured["kwargs"]["cwd"] == Path("C:/repos/sm64_tracker")
    assert captured["kwargs"]["creationflags"] == session.NEW_CONSOLE


def test_the_session_is_launched_through_a_console_host_this_app_controls():
    """Otherwise the window belongs to whatever the machine's default terminal is.

    Windows delegates every new console to that setting. When it names Windows
    Terminal, WT creates the window and `SW_SHOWNOACTIVATE` is discarded, so
    the session takes the keyboard — measured 2026-07-26, foreground moved
    within 400 ms and stayed. Going through conhost.exe opts out of the
    delegation and leaves the user's own terminal choice alone.
    """
    assert session.CONSOLE_HOST == "conhost.exe"


def test_the_new_console_opens_without_taking_focus(monkeypatch):
    # A session is opened mid-thought and mid-sentence. A console that
    # activates itself eats the next keystrokes and drops them into the new
    # session, so the window is asked to show without becoming active.
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kwargs: captured.update(kwargs))

    session.spawn_claude(Path("C:/repos/x"))

    startup = captured["startupinfo"]
    assert startup.dwFlags & subprocess.STARTF_USESHOWWINDOW
    assert startup.wShowWindow == session.SW_SHOWNOACTIVATE


def test_spawn_honours_a_launch_override(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kwargs: captured.update(args=args))

    session.spawn_claude(Path("C:/repos/x"), launch=["pwsh", "-c", "claude"])

    assert captured["args"] == [session.CONSOLE_HOST, "pwsh", "-c", "claude"]


def test_spawn_skips_permission_prompts_by_default(monkeypatch):
    captured = {}
    monkeypatch.setattr(subprocess, "Popen",
                        lambda args, **kwargs: captured.update(args=args))

    session.spawn_claude(Path("C:/repos/x"))

    assert captured["args"] == [session.CONSOLE_HOST, "claude",
                                "--dangerously-skip-permissions"]


def test_the_typist_is_given_the_process_inside_the_console(monkeypatch):
    # AttachConsole refuses a console host's own pid, and every consumer of it
    # fails quietly — so the wrong pid here costs the rename, the colour, the
    # prompt and the font, with nothing said.
    monkeypatch.setattr(session, "_children_of", lambda pid: [CLIENT_PID])

    assert session.session_pid(FakeHost()) == CLIENT_PID


def test_a_console_that_never_starts_a_session_falls_back_to_the_host(monkeypatch):
    # Quietly, like everything else on this path: the window is open, which is
    # the whole fallback contract.
    monkeypatch.setattr(session, "_children_of", lambda pid: [])

    assert session.session_pid(FakeHost(), timeout=0) == FakeHost.pid


def watching(monkeypatch, foreground, window=CONSOLE_WINDOW):
    """A session whose console window is `window` and foreground `foreground`."""
    handed = []
    monkeypatch.setattr(console_input, "console_window", lambda pid: window)
    monkeypatch.setattr(session, "foreground_window", lambda: foreground)
    monkeypatch.setattr(session, "_activate", handed.append)
    return handed


def test_the_keyboard_is_handed_back_if_the_new_console_takes_it(monkeypatch):
    # Asking for an unactivated window is not a guarantee: measured 2026-07-26,
    # two spawns in ten took the foreground anyway. Nothing this module opens
    # may take focus, so the ask is checked and undone.
    handed = watching(monkeypatch, foreground=CONSOLE_WINDOW)

    assert session.hold_focus(11, CLIENT_PID, seconds=0.05) is True
    assert handed == [11]


def test_a_window_the_user_moved_to_themselves_is_left_alone(monkeypatch):
    # Only this session's console counts as a thief. Someone who clicked away
    # to something else in the meantime keeps what they clicked on.
    handed = watching(monkeypatch, foreground=77)

    assert session.hold_focus(11, CLIENT_PID, seconds=0.05) is False
    assert handed == []


def test_nothing_is_handed_back_before_the_console_has_a_window(monkeypatch):
    handed = watching(monkeypatch, foreground=CONSOLE_WINDOW, window=0)

    assert session.hold_focus(11, CLIENT_PID, seconds=0.05) is False
    assert handed == []


def test_open_session_reports_the_process_inside_the_console(spawned):
    opened = claude_console.open_session(Path("C:/repos/x"))

    assert opened.pid == CLIENT_PID
    assert opened.host is not None


def test_open_session_starts_the_focus_watchdog_itself(monkeypatch):
    """The guarantee has to be structural, not a step a caller remembers.

    This is the reason `open_session` exists as one call rather than as
    spawn/resolve/watch. Before the extraction the watchdog was started by the
    single consumer's hand-off function, so a second consumer could simply not
    call it — and nothing would say so, because the failure is a window that
    steals focus two times in ten.
    """
    watched = {}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeHost())
    monkeypatch.setattr(session, "_children_of", lambda pid: [CLIENT_PID])
    monkeypatch.setattr(session, "foreground_window", lambda: 4321)
    monkeypatch.setattr(session, "hold_focus_in_background",
                        lambda previous, pid: watched.update(
                            previous=previous, pid=pid))

    claude_console.open_session(Path("C:/repos/x"))

    assert watched == {"previous": 4321, "pid": CLIENT_PID}


def test_the_window_watched_is_the_one_that_had_focus_before_the_spawn(monkeypatch):
    # Captured before, because after the spawn the window holding the keyboard
    # may already be the new console — and handing focus back to that is a
    # no-op that reads as the watchdog working.
    watched = {}
    foregrounds = iter([4321, CONSOLE_WINDOW, CONSOLE_WINDOW])
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeHost())
    monkeypatch.setattr(session, "_children_of", lambda pid: [CLIENT_PID])
    monkeypatch.setattr(session, "foreground_window", lambda: next(foregrounds))
    monkeypatch.setattr(session, "hold_focus_in_background",
                        lambda previous, pid: watched.update(previous=previous))

    claude_console.open_session(Path("C:/repos/x"))

    assert watched == {"previous": 4321}


def test_a_session_that_cannot_start_raises_rather_than_failing_quietly(monkeypatch):
    # Everything AFTER the process exists gives up quietly. The spawn itself
    # must not: a missing `claude` on PATH is the caller's to report.
    def exploding_popen(args, **kwargs):
        raise FileNotFoundError("claude is not on PATH")

    monkeypatch.setattr(subprocess, "Popen", exploding_popen)

    with pytest.raises(FileNotFoundError):
        claude_console.open_session(Path("C:/repos/x"))


def test_deliver_types_into_the_session_not_its_host(spawned):
    opened = claude_console.open_session(Path("C:/repos/x"))

    opened.deliver(prompt="BUG: body", commands=["/color red"])

    assert spawned == {"pid": CLIENT_PID, "commands": ["/color red"],
                       "text": "BUG: body"}


def test_deliver_needs_neither_a_prompt_nor_commands(spawned):
    # Opening a bare session is a legitimate use: the font still gets applied
    # and the watchdog still runs, which is why this is not an early return.
    opened = claude_console.open_session(Path("C:/repos/x"))

    opened.deliver()

    assert spawned == {"pid": CLIENT_PID, "commands": [], "text": ""}


def test_deliver_now_runs_on_this_thread_for_a_caller_about_to_exit(monkeypatch):
    # The CLI needs this: a daemon thread dies with the process that started
    # it, so the background form would type nothing at all from `-m`.
    delivered = {}
    monkeypatch.setattr(subprocess, "Popen", lambda *args, **kwargs: FakeHost())
    monkeypatch.setattr(session, "_children_of", lambda pid: [CLIENT_PID])
    monkeypatch.setattr(session, "hold_focus_in_background",
                        lambda previous, pid: None)
    monkeypatch.setattr(console_input, "deliver",
                        lambda pid, commands, text: delivered.update(
                            pid=pid, commands=commands, text=text))

    claude_console.open_session(Path("C:/repos/x")).deliver_now(prompt="hi")

    assert delivered == {"pid": CLIENT_PID, "commands": [], "text": "hi"}
