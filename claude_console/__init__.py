"""Open a visible Claude Code session and type into it, without taking focus.

Windows only. Everything here depends on Win32 — `conhost.exe`, console input
buffers, `CreateEnvironmentBlock` — and the module raises on import anywhere
else rather than pretending to be portable.

The whole surface is two calls::

    import claude_console

    session = claude_console.open_session(repo_root)
    session.deliver(prompt="FEATURE: make the thing")

`open_session` spawns the session, resolves the pid inside the console host,
and starts the focus watchdog before it returns. That last part is why it
exists as one call rather than three: the guarantee that nothing this opens
takes the keyboard is easy to state and easy to forget, so it is structural —
there is no way to get a `Session` without the watchdog running.

`deliver` is asynchronous. It waits for the session's prompt box to appear
before typing (which can take tens of seconds on a cold start), so it runs on
a daemon thread and reports nothing back. Every failure on that path is quiet
by design: the caller is expected to have put the same text somewhere the user
can reach it, and a timeout then costs one Ctrl+V rather than the text itself.

For lower-level work — submitting a single command, changing the console font,
spawning something that is not Claude — reach past this into `session` and
`console_input`, both of which are public.
"""

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import console_input
from . import environment
from . import session as _session
from .console_input import SESSION_FACE, SESSION_IMAGE, use_font, use_icon
from .environment import login_environment
from .session import (
    CLIENT_POLL,
    CLIENT_TIMEOUT,
    CONSOLE_HOST,
    DEFAULT_LAUNCH,
    FOREGROUND_POLL,
    FOREGROUND_WATCH,
    SW_SHOWNOACTIVATE,
    claude_environment,
    foreground_window,
    hold_focus,
    hold_focus_in_background,
    session_pid,
    spawn_claude,
    unfocused_startup,
)
from .text import SESSION_NAME_LIMIT, cap, safe_line

# `session.NEW_CONSOLE` is deliberately NOT re-exported here. Nothing outside
# `spawn_claude` should be asking for a new console, and this package's own
# conventions test greps for the name in every spelling — including a string
# constant, which is how the flag is really written
# (`getattr(subprocess, "CREATE_NEW_CONSOLE", 0)`). Listing it in `__all__`
# tripped that guard, which was the guard doing its job: a re-export is an
# invitation, and the one legitimate use already lives one import away at
# `claude_console.session.NEW_CONSOLE`.
__all__ = [
    "Session", "open_session",
    # The pieces, for callers that need one without the other.
    "console_input", "environment",
    "spawn_claude", "session_pid", "unfocused_startup",
    "foreground_window", "hold_focus", "hold_focus_in_background",
    "claude_environment", "login_environment",
    "safe_line", "cap", "SESSION_NAME_LIMIT",
    "use_font", "SESSION_FACE", "use_icon", "SESSION_IMAGE",
    "CONSOLE_HOST", "DEFAULT_LAUNCH", "SW_SHOWNOACTIVATE",
    "CLIENT_TIMEOUT", "CLIENT_POLL", "FOREGROUND_WATCH", "FOREGROUND_POLL",
]


@dataclass(frozen=True)
class Session:
    """A Claude session running in a console this process can type into.

    `pid` is the session itself — conhost's child — and is the only pid that
    works for anything in `console_input`: `AttachConsole` refuses a console
    host's own pid. `host` is the `Popen` of the host, kept because it is the
    handle to the actual process tree and a caller may want to wait on it.
    """

    pid: int
    host: subprocess.Popen

    def deliver(self, prompt: str = "",
                commands: list[str] | tuple[str, ...] = ()) -> threading.Thread:
        """Submit each command, then leave `prompt` typed in the box unsent.

        Returns immediately; the work happens on a daemon thread. Commands go
        first and are each followed by Enter; the prompt is pasted last and is
        deliberately *not* submitted, so the user reads it and sends it
        themselves. A command that fails to submit costs only itself — the
        prompt is pasted regardless, because it was never made to depend on
        the commands succeeding.
        """
        return console_input.deliver_when_ready(self.pid, list(commands), prompt)

    def deliver_now(self, prompt: str = "",
                    commands: list[str] | tuple[str, ...] = ()) -> None:
        """`deliver`, on this thread. For a caller that is about to exit.

        A process that spawns a session and returns immediately kills the
        daemon thread that was going to do the typing, so the CLI needs this
        and a long-lived app does not.
        """
        console_input.deliver(self.pid, list(commands), prompt)

    def window(self) -> int:
        """The console's window handle, or 0 before it has one."""
        return console_input.console_window(self.pid)


def open_session(cwd: Path | str, launch: list[str] | None = None) -> Session:
    """Open a visible Claude session in `cwd`, and never take the keyboard.

    `launch` overrides the argv, which defaults to
    `["claude", "--dangerously-skip-permissions"]`. Whatever it names is run
    inside a console host this asks for by name, so the window is a classic
    console regardless of what the machine's default terminal is set to — see
    `session.CONSOLE_HOST` for the measurement behind that.

    Three things happen here in an order that matters. The window holding the
    keyboard is recorded *before* the spawn, because afterwards it may already
    be the new one. The pid is then resolved to the process inside the host,
    since that is the only one that can be typed into. And the watchdog starts
    last, needing both.

    Raises whatever `Popen` raises — a missing `claude` on PATH is a
    `FileNotFoundError` and the caller should see it. Everything *after* the
    process exists fails quietly instead.

    Every call below goes through `_session.` rather than the names this module
    re-exports, and that is not style. `from .session import foreground_window`
    binds the function *object* into this namespace at import, so a test that
    patches `session.hold_focus_in_background` would patch a name this function
    never reads — and the watchdog would go on running for real, in a suite
    that believes it stubbed it out. That failed here first time: the two tests
    asserting the watchdog starts both saw an empty recorder while the real
    thing attached to a console underneath them.
    """
    previous_window = _session.foreground_window()
    host = _session.spawn_claude(Path(cwd), launch)
    pid = _session.session_pid(host)
    _session.hold_focus_in_background(previous_window, pid)
    return Session(pid=pid, host=host)
