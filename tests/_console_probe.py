"""Two halves of the console-typing test, kept out of the pytest process.

`paste` attaches this process to someone else's console, which means leaving
its own — pytest's terminal would go with it. Running both halves here keeps
that where it belongs: in a throwaway process.

  child   prints a fake "ready" prompt, then logs every character it reads
  parent  opens the child in a new console, types into it, prints the log
"""

import subprocess
import sys
import time
from pathlib import Path

# No sys.path juggling: the package is installed editable, so the child process
# this file re-launches resolves the very same source tree the parent imported.
from claude_console import console_input

# A real console for the child, with no window on screen. CREATE_NO_WINDOW still
# allocates a genuine console — AttachConsole, WriteConsoleInput and the screen
# buffer all behave exactly as they do for a visible one — it just never shows
# the host window, which is the whole of what this test needs.
#
# It used to be CREATE_NEW_CONSOLE plus launcher.unfocused_startup(), on the
# theory that SW_SHOWNOACTIVATE made the window harmless. It does not, because
# Windows 11 delegates new consoles to whatever is set as the default terminal
# application. When that is Windows Terminal, WT creates the window itself and
# the spawner's STARTUPINFO never reaches it: a full Terminal window opens,
# activated, for as long as the child lives. Measured 2026-07-25 — every run of
# this suite flashed one, which is most of what "random windows keep popping up
# while Claude works" turned out to be.
CONSOLE_FLAGS = subprocess.CREATE_NO_WINDOW


def run_child(log_path: str) -> None:
    import msvcrt

    # One of console_input.READY_MARKERS: the parent waits to see it before
    # typing, exactly as it waits for a real session's prompt box.
    print("? for shortcuts", flush=True)
    seen = []
    deadline = time.time() + 15
    while time.time() < deadline and console_input.PASTE_END not in "".join(seen):
        if msvcrt.kbhit():
            seen.append(msvcrt.getwch())
        else:
            time.sleep(0.01)
    Path(log_path).write_text("".join(seen), encoding="utf-8", newline="\n")


def run_parent() -> None:
    log_path = str(Path(__file__).with_name("_console_probe_log.txt"))
    Path(log_path).unlink(missing_ok=True)
    # Windowless: a test that puts anything on screen is exactly the behaviour
    # the code under test forbids, and this one ran on every suite invocation.
    child = subprocess.Popen(
        [sys.executable, __file__, "child", log_path],
        creationflags=CONSOLE_FLAGS)
    typed = console_input.paste(child.pid, "hello there", timeout=20)
    child.wait(timeout=30)
    received = Path(log_path).read_text(encoding="utf-8")
    Path(log_path).unlink(missing_ok=True)
    # A new console arrives with a few stray characters of its own in the
    # input buffer, so what matters is that the payload is delivered whole and
    # unbroken — which is what the paste markers around it are for.
    payload = console_input.PASTE_START + "hello there" + console_input.PASTE_END
    print(f"typed={typed}")
    print(f"delivered={payload in received}")
    print(f"received={received!r}")


if __name__ == "__main__":
    if sys.argv[1] == "child":
        run_child(sys.argv[2])
    else:
        run_parent()
