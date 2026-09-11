"""Terminal.app handoff: private PTY input, terminal-visible confirmation.

Only ``open_terminal`` opens a window, in response to the consumer's user
gesture. The PTY owner runs in a separate interpreter: pty.fork is unsafe in
a Cocoa process. Terminal's scripting dictionary defines ``contents`` as the
visible tab text (``history`` includes scrollback and must not be used here).
"""
import json
import os
import select
import shlex
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from . import journal

HELPER = Path(__file__).with_name("_mac_relay.py")
_sessions = {}
_current = threading.local()


def default_launch(name=""):
    from .session import display_name
    named = display_name(name)
    command = ["claude", "--dangerously-skip-permissions"]
    if named:
        command += ["-n", named]
    return ["/bin/zsh", "-l", "-i", "-c", shlex.join(command) + "; exec /bin/zsh -l"]


def login_environment():
    """Rebuild from the account and its login shell, never the GUI's parent.

    The allowlisted seed contains identity and terminal capabilities only;
    the user's login files own additions such as PATH. No agent environment
    is inherited or repaired with an upstream-variable denylist.
    """
    import pwd
    account = pwd.getpwuid(os.getuid())
    seed = {"HOME": account.pw_dir, "USER": account.pw_name,
            "LOGNAME": account.pw_name, "SHELL": account.pw_shell or "/bin/zsh",
            "PATH": "/usr/bin:/bin:/usr/sbin:/sbin", "TERM": "xterm-256color",
            "LANG": "en_US.UTF-8", "TMPDIR": tempfile.gettempdir()}
    # zsh reads .zshrc only when interactive. A login-only shell missed the
    # installed Claude path on the first Mac probe. Delimit the environment
    # because an interactive profile is allowed to print a greeting.
    command = "printf '\\0CLAUDE_CONSOLE_ENV\\0'; /usr/bin/env -0"
    result = subprocess.run([seed["SHELL"], "-l", "-i", "-c", command],
                            env=seed, cwd=account.pw_dir, stdin=subprocess.DEVNULL,
                            capture_output=True,
                            timeout=15, check=True)
    environment = {}
    _, separator, output = result.stdout.partition(b"\0CLAUDE_CONSOLE_ENV\0")
    if not separator:
        raise OSError("The login shell did not return its environment")
    for entry in output.decode("utf-8").split("\0"):
        key, separator, value = entry.partition("=")
        if separator and key and "\n" not in key:
            environment[key] = value
    if not all(environment.get(key) for key in ("HOME", "PATH", "USER")):
        raise OSError("The login shell did not return a usable environment")
    return environment


def _applescript(source, *arguments):
    result = subprocess.run(["/usr/bin/osascript", "-", *arguments],
                            input=source, capture_output=True, text=True,
                            timeout=15, check=False)
    if result.returncode:
        raise OSError("Terminal automation failed. Allow this app to control "
                      "Terminal in System Settings > Privacy & Security > "
                      "Automation, then retry. " + result.stderr.strip())
    return result.stdout.rstrip("\n")


def open_terminal(socket_path):
    """Open one new tab/window and return its stable TTY, never the front tab."""
    command = shlex.join([sys.executable, str(HELPER), "attach", str(socket_path)])
    return _applescript('''on run argv
tell application "Terminal"
    set sessionTab to do script ("exec " & item 1 of argv)
    activate
    return tty of sessionTab
end tell
end run''', command)


def terminal_contents(tty):
    return _applescript('''on run argv
tell application "Terminal"
    repeat with sessionWindow in windows
        repeat with sessionTab in tabs of sessionWindow
            if tty of sessionTab is item 1 of argv then
                return contents of sessionTab
            end if
        end repeat
    end repeat
end tell
error "The session's Terminal tab has closed"
end run''', tty)


def prompt_ready(screen):
    from .console_input import BOX_MARKERS, READY_MARKERS
    rows = screen.replace("\r\n", "\n").replace("\r", "\n").splitlines()
    # A footer alone may be shell echo or a transcript. Require the actual
    # input row ABOVE it, and explicitly refuse startup/permission dialogs.
    lower = screen.lower()
    if any(text in lower for text in ("do you trust", "is this a project you trust",
                                      "trust this folder", "yes, i trust",
                                      "yes, allow", "do you want to proceed")):
        return False
    boxes = [i for i, row in enumerate(rows) if row.startswith(BOX_MARKERS)]
    return bool(boxes) and any(marker in row for row in rows[boxes[-1] + 1:]
                               for marker in READY_MARKERS)


def request(path, message):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.settimeout(5)
        connection.connect(str(path))
        connection.sendall(json.dumps(message).encode() + b"\n")
        with connection.makefile("rb") as response:
            return json.loads(response.readline(1024 * 1024))


def _single_line_prompt(screen):
    """Read an entire simple input region; ambiguous/wrapped content is unsafe.

    The shared protocol recognises an opening substring to confirm a paste.
    That is insufficient authorization for Enter or Ctrl+U: another person's
    text could surround it, including on continuation rows below it.
    """
    from .console_input import BOX_MARKERS, READY_MARKERS, prompt_box
    rows = screen.splitlines()
    boxes = [i for i, row in enumerate(rows) if row.startswith(BOX_MARKERS)]
    if not boxes:
        return None
    for row in rows[boxes[-1] + 1:]:
        if any(marker in row for marker in READY_MARKERS):
            return prompt_box(screen)
        # The supported layouts put a horizontal input separator before the
        # status footer. Unknown content is not silently discarded as chrome.
        if row.strip() and not all(char in " ─━╰╯└┘│" for char in row):
            return None
    return None


@dataclass
class Transport:
    host: subprocess.Popen
    path: Path
    tty: str = ""
    last_error: str = ""
    input_generation: int | None = None
    owned_line: str | None = None

    def screen(self):
        text = terminal_contents(self.tty).replace("\r\n", "\n").replace("\r", "\n")
        if not prompt_ready(text):
            # Keep the failure screen readable in the journal, while ensuring
            # the shared Windows readiness heuristic cannot accept a dialog.
            from .console_input import READY_MARKERS
            for marker in READY_MARKERS:
                text = text.replace(marker, "[inactive prompt hint]")
        return text

    def write(self, text):
        # Recheck at the write boundary. In particular, never send a command's
        # Enter to a trust/permission dialog that replaced the prompt.
        # Capture before reading the screen. A generation captured afterwards
        # would adopt input typed during that read as our own safe baseline.
        # Never refresh it on retries: a user's intervention cancels the rest
        # of this handoff rather than authorizing it again.
        if self.input_generation is None:
            self.input_generation = request(self.path, {"status": True})["input_generation"]
        screen = terminal_contents(self.tty).replace("\r\n", "\n").replace("\r", "\n")
        if not prompt_ready(screen):
            return False
        from .console_input import CLEAR_LINE, PASTE_START, PASTE_END
        box = _single_line_prompt(screen)
        pasted = text[len(PASTE_START):-len(PASTE_END)] if text.startswith(PASTE_START) and text.endswith(PASTE_END) else None
        if text == "\r":
            if self.owned_line is None or box != self.owned_line:
                return False
        elif text == CLEAR_LINE:
            if box != "" and (self.owned_line is None or box != self.owned_line):
                return False
        elif pasted is not None:
            if box is None or box and not box.startswith('Try "'):
                return False
        elif text != PASTE_END:
            return False
        written = request(self.path, {"write": text, "input_generation": self.input_generation}).get("written") == len(text.encode("utf-8"))
        if written and pasted is not None:
            self.owned_line = pasted.strip() if "\n" not in pasted and "\r" not in pasted else None
        return written


def spawn_claude(cwd, launch=None, name=""):
    cwd = Path(cwd).resolve(strict=True)
    if not cwd.is_dir():
        raise NotADirectoryError(cwd)
    environment = login_environment()
    argv = list(launch or default_launch(name))
    if not argv or not all(isinstance(arg, str) and "\0" not in arg for arg in argv):
        raise ValueError("launch must be a nonempty list of command arguments")
    executable = argv[0]
    found = (os.path.abspath(cwd / executable) if "/" in executable
             else shutil.which(executable, path=environment["PATH"]))
    if not found or not os.path.isfile(found) or not os.access(found, os.X_OK):
        raise FileNotFoundError(f"Session executable is not available: {executable}")
    if not launch and not shutil.which("claude", path=environment["PATH"]):
        raise FileNotFoundError("Claude is not on your login shell's PATH")
    argv[0] = found
    # /tmp keeps the Unix socket path below macOS's 104-byte limit even when
    # the repository lives in a deeply nested worktree. mkdtemp is mode 0700.
    directory = Path(tempfile.mkdtemp(prefix="claude-console-", dir="/tmp"))
    path = directory / "relay.sock"
    config = directory / "session.json"
    config.write_text(json.dumps({"cwd": str(cwd), "argv": argv,
                                 "env": environment}), encoding="utf-8")
    host = None
    try:
        host = subprocess.Popen([sys.executable, str(HELPER), "serve", str(config)],
                                stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, start_new_session=True)
        ready, _, _ = select.select([host.stdout], [], [], 10)
        if not ready or host.stdout.readline() != b"ready\n":
            detail = (host.stderr.read().decode("utf-8", errors="replace").strip()
                      if host.poll() is not None else "Startup timed out")
            raise OSError("The session relay could not start. " + detail)
        host.stdout.close()
        transport = Transport(host, path)
        transport.tty = open_terminal(path)
        if not transport.tty.startswith("/dev/"):
            raise OSError("Terminal did not identify the session tab")
        _sessions[host.pid] = transport

        def reap():
            detail = host.stderr.read().decode("utf-8", errors="replace").strip()
            host.stderr.close()
            host.wait()
            if detail:
                journal.record(f"pid {host.pid}: relay exited: {detail}")
            _sessions.pop(host.pid, None)
            shutil.rmtree(directory, ignore_errors=True)
        threading.Thread(target=reap, daemon=True).start()
        return host
    except BaseException:
        if host is not None:
            host.terminate()
            try:
                host.wait(timeout=5)
            except subprocess.TimeoutExpired:
                host.kill()
                host.wait()
            if host.stdout:
                host.stdout.close()
            if host.stderr:
                host.stderr.close()
        shutil.rmtree(directory, ignore_errors=True)
        raise


def attached(pid):
    transport = _sessions.get(pid)
    _current.transport = transport
    return transport is not None and transport.host.poll() is None


def screen_text():
    return _guard(lambda transport: transport.screen(), "")


def write_input(text):
    return _guard(lambda transport: transport.write(text), False)


def _guard(action, fallback):
    transport = getattr(_current, "transport", None)
    if transport is None:
        return fallback
    try:
        return action(transport)
    except Exception as error:
        message = str(error)
        if message != transport.last_error:
            journal.record(f"pid {transport.host.pid}: {message}")
            transport.last_error = message
        return fallback
