"""Open a console running Claude Code, in the terminal this machine uses.

Everything here is a Windows finding that cost a debugging session to discover.
The comments carry the measurements, and they are the reason this module is
shared rather than reimplemented: the code is short enough to rewrite from
scratch and the reasoning is not.

The calling process must not own a console it still needs — see console_input,
which does the attaching.
"""

import subprocess
from pathlib import Path

from . import environment

NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

# Claude, inside the shell this machine develops in.
#
# `-NoExit` is the half of this that is not obvious: without it the window and
# its whole scrollback vanish the instant Claude exits, and with it you are left
# at a PowerShell prompt already sitting in the session's directory. The cost is
# that a finished session leaves a window behind until it is closed.
#
# The last element is one string on purpose. `-Command` takes the rest of the
# line as a command, and Popen quotes this element as a unit, so Claude's own
# `--` arguments reach Claude rather than being read by PowerShell.
#
# The profile is deliberately NOT suppressed. Matching the shell its user
# already has is the entire point; a slow profile costs a moment of startup and
# nothing else.
DEFAULT_LAUNCH = [
    "powershell.exe", "-NoExit", "-Command",
    "claude --dangerously-skip-permissions",
]

# Nothing is prepended to that command, and the absence is the design.
#
# `conhost.exe` used to be, to opt out of Windows' default-terminal delegation:
# Windows hands every *new* console to whatever `HKCU\\Console\\%%Startup` names,
# and when that is Windows Terminal, WT creates the window itself, so a
# spawner's `STARTUPINFO` never reaches it. That pin bought control of the
# window and paid for it in glyphs. conhost can only use a monospaced font, and
# **no monospaced font on this machine covers `U+23BF`** — the `⎿` Claude Code
# draws on every tool result line. Measured 2026-07-26 across every installed
# font via `GlyphTypeface.CharacterToGlyphMap`: exactly three cover it (Noto
# Sans JP, Noto Serif JP, Segoe UI Symbol) and all three are proportional.
#
# So the window goes back to the machine's own setting. What was given up is
# the focus guarantee, which turned out to be protecting a case nobody wanted
# protected — see `unfocused_startup` — and the taskbar icon, which is a known
# loss: `WM_SETICON` on the real Terminal window does change the window icon
# (measured: handles became claude.exe's exactly) and the taskbar still draws
# Terminal's, because a packaged app's button follows its AUMID manifest.

# Show a window without making it the active one. Nothing in this module passes
# this any more — a session is opened by a human pressing a button, and that
# gesture earns the focus it takes.
#
# It survives for the other half of the rule, which did not change: a window the
# user did NOT ask for may never activate. task_tracker's `restart.py` relaunches
# the tracker through this, and anything this machine spawns for its own
# purposes should too. The rule is "focus is opt-in", not "focus never happens".
SW_SHOWNOACTIVATE = 4


def unfocused_startup() -> "subprocess.STARTUPINFO":
    """STARTUPINFO for a window that appears without becoming active.

    Exposed rather than kept private because it is useful to anything this
    machine spawns for itself, not only to a Claude session — task_tracker's
    `restart.py` relaunches the tracker through it and opens no console at all.
    """
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = SW_SHOWNOACTIVATE
    return startup


def claude_environment() -> dict[str, str]:
    """The environment a session you opened by hand would have.

    Not this process's environment with the awkward parts removed — the
    spawning process is usually itself started from a Claude session, and
    inheriting from it is the whole problem. See `environment` for why this is
    rebuilt rather than filtered.
    """
    return environment.login_environment()


def spawn_claude(cwd: Path,
                 launch: list[str] | None = None) -> subprocess.Popen:
    """Start a Claude session in `cwd`, in a new console.

    `CREATE_NEW_CONSOLE` from a console-less parent is what reaches Windows'
    default-terminal handoff, which is how the window becomes the terminal its
    user actually develops in.

    `launch` replaces the whole argv, PowerShell wrapper included: a caller that
    names its own shell gets exactly what it named.

    Returns the session's own process — the pid to type into. Callers should
    still prefer `claude_console.open_session`, which is the composed call.
    """
    return subprocess.Popen(
        launch or DEFAULT_LAUNCH,
        cwd=Path(cwd),
        creationflags=NEW_CONSOLE,
        env=claude_environment(),
    )


def session_pid(host: subprocess.Popen) -> int:
    """The pid to type into, which is the spawned process itself.

    This used to walk the process tree, and had to: `spawn_claude` started
    conhost, conhost started the session, and `AttachConsole` refuses a console
    host's own pid (measured: false for conhost, true for its child). With no
    host in between there is nothing to walk past, and walking anyway is
    actively wrong — measured 2026-07-26, the first child of a freshly spawned
    shell was an incidental helper it had started, so the walk returned a pid
    that could not be typed into.

    The PowerShell wrapper does not bring the problem back. `powershell.exe`
    shares the console it was given rather than making one, so attaching to it
    reaches the same screen buffer `claude` is painting.

    Kept as a named call rather than inlined so `open_session` still reads as
    spawn-then-resolve, and so a consumer holding a bare `Popen` has one
    obvious way to ask.
    """
    return host.pid
