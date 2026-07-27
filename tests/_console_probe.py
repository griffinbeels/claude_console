"""Two halves of the console-typing test, kept out of the pytest process.

`paste` attaches this process to someone else's console, which means leaving
its own — pytest's terminal would go with it. Running both halves here keeps
that where it belongs: in a throwaway process.

  child   prints a fake "ready" prompt, draws what it reads into a prompt-box
          row the way a session does, and logs every character it received
  parent  opens the child in a new console, types into it, prints the log

The child echoes because `paste` now *confirms* — it reads the box back and
only reports success once its own text is showing there. A child that read
without drawing would fail that confirmation forever, and the honest fix is
for the fake session to behave like the real one rather than for the test to
opt out of the check. The layout it draws is the one captured from a live
session in test_console_input.py.
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


# How long the child holds its console open after the paste completes. The
# parent reads the box back to confirm delivery, and a console that vanishes
# the instant the last character arrives cannot be read at all. This paces
# nothing: it keeps an artifact alive for the observer, which is the opposite
# of the sleep invariant 8 forbids.
LINGER_SECONDS = 3.0


def draw_box(seen: list[str]) -> None:
    """Show what has been received the way a session shows its prompt box.

    Nothing is drawn until the opening marker has arrived in full, and only
    printable characters are drawn — a half-received `ESC [ 2 0 0 ~` written
    to the screen would be interpreted as a terminal escape rather than shown.
    """
    text = "".join(seen)
    if console_input.PASTE_START not in text:
        return
    body = text.split(console_input.PASTE_START, 1)[1]
    body = body.split(console_input.PASTE_END, 1)[0]
    print("\r> " + "".join(char for char in body if char.isprintable()),
          end="", flush=True)


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
            draw_box(seen)
        else:
            time.sleep(0.01)
    holding = time.time() + LINGER_SECONDS
    while time.time() < holding:
        if msvcrt.kbhit():
            seen.append(msvcrt.getwch())
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
    # One paste, not three: the confirmation saw the text in the box, so the
    # retry never ran. This is the half a mocked screen cannot prove — that
    # what `box_shows` reads back is what a real console really shows.
    print(f"pastes={received.count(console_input.PASTE_START)}")
    print(f"received={received!r}")


if __name__ == "__main__":
    if sys.argv[1] == "child":
        run_child(sys.argv[2])
    else:
        run_parent()
