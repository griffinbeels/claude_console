"""Open a console running Claude Code, and keep it off the keyboard.

Everything here is a Windows finding that cost a debugging session to discover.
The comments carry the measurements, and they are the reason this module is
shared rather than reimplemented: the code is short enough to rewrite from
scratch and the reasoning is not.

The calling process must not own a console it still needs — see console_input,
which does the attaching.
"""

import ctypes
import subprocess
import threading
import time
from ctypes import wintypes
from pathlib import Path

from . import console_input
from . import environment

NEW_CONSOLE = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)

DEFAULT_LAUNCH = ["claude", "--dangerously-skip-permissions"]

# Launch the session *through* conhost.exe, so the window this opens is a
# classic console it can actually control.
#
# Windows hands every new console to whatever is set as the default terminal
# application. That setting is machine-wide, it belongs to the user rather than
# to any one app, and it has already changed underneath a consumer of this code
# once. When it names Windows Terminal, WT creates the window itself — the
# `SW_SHOWNOACTIVATE` below never reaches it, and the hand-off takes the
# keyboard mid-sentence. Measured 2026-07-26 from a console-less parent, default
# terminal set to Windows Terminal: spawning the command directly moved the
# foreground to `CASCADIA_HOSTING_WINDOW_CLASS` within 400 ms and kept it;
# spawning the same command through `conhost.exe` left the foreground untouched
# for the whole run and produced a `ConsoleWindowClass` window.
#
# This pins only the window this module opens. Whatever the user has chosen for
# every other console on the machine is none of its business, and stays.
CONSOLE_HOST = "conhost.exe"

# How long to wait for the console host to start the session inside it, and how
# often to look. Measured well under a tenth of a second; this is the
# "something is wrong" bound, not a normal wait.
CLIENT_TIMEOUT = 5.0
CLIENT_POLL = 0.02

# How long to keep an eye on the foreground after a spawn, and how often.
# The theft, when it happens, is there inside three tenths of a second — the
# rest is margin. Short on purpose: past this the window belongs to the user,
# and clicking it is a thing they are allowed to do.
FOREGROUND_WATCH = 1.5
FOREGROUND_POLL = 0.05

_TH32CS_SNAPPROCESS = 0x00000002

_user32 = ctypes.WinDLL("user32", use_last_error=True)

# Show the new console, but do not make it the active window (SW_SHOWNOACTIVATE
# rather than the default SW_SHOWNORMAL). A session is opened while you are
# mid-thought: a window that grabs focus swallows whatever you type next and
# puts it into a session you were not looking at. Nothing here needs focus
# — console_input writes to the console's input buffer, which does not require
# the window to be active.
SW_SHOWNOACTIVATE = 4


def unfocused_startup() -> "subprocess.STARTUPINFO":
    """STARTUPINFO for a window that appears without becoming active.

    Exposed rather than kept private because it is useful to anything this
    machine spawns, not only to a Claude session — task_tracker's `restart.py`
    relaunches the tracker itself through it and opens no console at all.
    """
    startup = subprocess.STARTUPINFO()
    startup.dwFlags |= subprocess.STARTF_USESHOWWINDOW
    startup.wShowWindow = SW_SHOWNOACTIVATE
    return startup


def foreground_window() -> int:
    """Whatever window has the keyboard right now."""
    return _user32.GetForegroundWindow()


def _activate(window: int) -> None:
    _user32.SetForegroundWindow(wintypes.HWND(window))


def hold_focus(previous: int, session: int,
               seconds: float = FOREGROUND_WATCH) -> bool:
    """Give the keyboard back if the session's console takes it.

    `unfocused_startup` *asks* for a window that does not activate, and going
    through `conhost.exe` is what makes the request reach the right process —
    but the request is not always honoured. Measured 2026-07-26 over ten
    spawns: two took the foreground anyway, apparently depending on how
    promptly whatever was in front was answering messages. Two in ten is not a
    guarantee, and the rule is that nothing this opens may take focus, so
    asking is not enough: check, and undo it.

    Two things keep this from becoming a window that fights its user. It hands
    back only when the thief is *this* session's console, so clicking away to
    something else in the meantime is respected; and it does so at most once,
    inside a window shorter than it takes to reach for the mouse. Deliberately
    clicking the new session is a human gesture and earns the focus it asks
    for — this only reverses focus nobody asked for.

    Returns whether it had to hand anything back, which is the only way to
    know from outside whether the spawn misbehaved this time.
    """
    window = console_input.console_window(session)
    if not previous or not window:
        return False
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if foreground_window() == window:
            _activate(previous)
            return True
        time.sleep(FOREGROUND_POLL)
    return False


def hold_focus_in_background(previous: int, session: int) -> threading.Thread:
    """Watch the foreground off the caller's thread, as the typing is watched."""
    watcher = threading.Thread(target=hold_focus, args=(previous, session),
                               daemon=True)
    watcher.start()
    return watcher


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
    """Start a Claude session in `cwd`, inside a console host we named.

    Low level on purpose: this returns the *host* process, which is not the pid
    anything can type into. Callers should prefer `claude_console.open_session`,
    which resolves the session inside it and starts the focus watchdog.
    """
    return subprocess.Popen(
        [CONSOLE_HOST] + (launch or DEFAULT_LAUNCH),
        cwd=Path(cwd),
        creationflags=NEW_CONSOLE,
        env=claude_environment(),
        startupinfo=unfocused_startup(),
    )


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [("dwSize", wintypes.DWORD), ("cntUsage", wintypes.DWORD),
                ("th32ProcessID", wintypes.DWORD),
                ("th32DefaultHeapID", ctypes.POINTER(ctypes.c_ulong)),
                ("th32ModuleID", wintypes.DWORD), ("cntThreads", wintypes.DWORD),
                ("th32ParentProcessID", wintypes.DWORD),
                ("pcPriClassBase", wintypes.LONG), ("dwFlags", wintypes.DWORD),
                ("szExeFile", wintypes.WCHAR * 260)]


def _children_of(parent_pid: int) -> list[int]:
    """The pids Windows currently reports as children of `parent_pid`."""
    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
    entry = PROCESSENTRY32W()
    entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
    children = []
    if kernel32.Process32FirstW(snapshot, ctypes.byref(entry)):
        while True:
            if entry.th32ParentProcessID == parent_pid:
                children.append(entry.th32ProcessID)
            if not kernel32.Process32NextW(snapshot, ctypes.byref(entry)):
                break
    kernel32.CloseHandle(snapshot)
    return children


def session_pid(host: subprocess.Popen, timeout: float = CLIENT_TIMEOUT) -> int:
    """The pid to type into: the process *inside* the console, not its host.

    `spawn_claude` starts conhost, and conhost starts the session — so the
    Popen names the host, and `AttachConsole` refuses a host's pid (measured:
    it returns false for conhost and true for its child). Handing the wrong
    one on would silently cost the rename, the colour, the typed prompt and
    the console font all at once, since every one of those fails quietly by
    design.

    The first child is the right one whatever `launch` was: a per-project
    override like `pwsh -c claude` makes pwsh the child, and pwsh shares the
    console it was given, so attaching to it reaches the same screen buffer.

    Falls back to the host's own pid rather than raising. Everything
    downstream already gives up quietly, so a session that somehow never
    appears still costs a window, not an error.
    """
    deadline = time.monotonic() + timeout
    while True:
        children = _children_of(host.pid)
        if children:
            return children[0]
        if time.monotonic() >= deadline:
            return host.pid
        time.sleep(CLIENT_POLL)
