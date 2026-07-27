"""Open a visible Claude Code session and type into it.

Windows only. Everything here depends on Win32 — the default-terminal handoff,
console input buffers, `CreateEnvironmentBlock` — and the module raises on
import anywhere else rather than pretending to be portable.

The whole surface is two calls::

    import claude_console

    session = claude_console.open_session(repo_root)
    session.deliver(prompt="FEATURE: make the thing")

`open_session` spawns the session and hands back the pid to type into. The
window it opens belongs to whatever this machine's default terminal is — here,
Windows Terminal — and it is allowed to come to the front, because opening a
session is a human gesture and one earns the focus it takes. What must open
nothing at all is a *test*, and that is enforced in `tests/test_conventions.py`.

`deliver` is asynchronous. It waits for the session's prompt box to appear
before typing (which can take tens of seconds on a cold start), so it runs on
a daemon thread and reports nothing back. Every failure on that path is quiet
by design: the caller is expected to have put the same text somewhere the user
can reach it, and a timeout then costs one Ctrl+V rather than the text itself.

For lower-level work — submitting a single command, reading what a session is
showing, spawning something that is not Claude — reach past this into `session`
and `console_input`, both of which are public.
"""

import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path

from . import console_input
from . import environment
from . import session as _session
from .environment import login_environment
from .session import (
    DEFAULT_LAUNCH,
    SW_SHOWNOACTIVATE,
    claude_environment,
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
    "claude_environment", "login_environment",
    "safe_line", "cap", "SESSION_NAME_LIMIT",
    "DEFAULT_LAUNCH", "SW_SHOWNOACTIVATE",
]


@dataclass(frozen=True)
class Session:
    """A Claude session running in a console this process can type into.

    `pid` is the session itself, which is now the spawned process rather than a
    child of it — see `session.session_pid` for why that changed. `host` is the
    `Popen` behind it, kept under that name because a caller may want to wait
    on it, and renaming it would break a consumer for no gain.
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

    `launch` overrides the argv, which defaults to `claude` running inside
    PowerShell. Nothing is prepended to it: the console goes to whatever this
    machine's default terminal is, which is the point rather than a compromise
    — see `session.DEFAULT_LAUNCH` for the measurement behind that.

    Raises whatever `Popen` raises — a missing `claude` on PATH is a
    `FileNotFoundError` and the caller should see it. Everything *after* the
    process exists fails quietly instead.

    Both calls below go through `_session.` rather than the names this module
    re-exports, and that is not style. `from .session import session_pid` binds
    the function *object* into this namespace at import, so a test patching
    `session.session_pid` would patch a name this function never reads, and the
    real one would run underneath a suite that believes it stubbed it out. That
    failed here first time, against the focus watchdog this call used to start,
    and two tests caught it.
    """
    host = _session.spawn_claude(Path(cwd), launch)
    return Session(pid=_session.session_pid(host), host=host)
