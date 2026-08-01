"""Type text into another process's console window, the way a keyboard would.

Claude Code has no flag that pre-fills its prompt box. A positional prompt is
*submitted* the instant the session opens, and there is nothing in between — so
the only way to hand a fresh session text it has not yet sent is to deliver it
as console input. Windows allows exactly that: attach to another process's
console and write into its input buffer.

Four things this module has to get right, each learned by watching a real
session receive the text:

* **Bracketed paste.** The text is wrapped in the markers a terminal emits for
  Ctrl+V (`ESC [ 200 ~` … `ESC [ 201 ~`). Without them a newline mid-text reads
  as Enter and submits the first task on its own.
* **Wait for the prompt box.** Input written earlier is not lost — it queues in
  the console input buffer — but it can be *consumed by whatever is on screen
  first*. A folder Claude has not been trusted in opens on "Is this a project
  you trust?", whose default answer is Enter. So poll until the session's
  status hint proves the prompt box is up, and only then type.
* **Wait between every write, for the screen and not for the clock.** Two
  writes the session reads in one go are not two events to it. Measured
  against a live session: writing the whole hand-off back to back put
  `/rename …` and `/color green` on one line with *both* Enters discarded — a
  `\r` between two bracketed pastes is read as part of the paste. The gap that
  matters is not how long the writer waited, it is whether the session has
  drained the buffer, and the prompt box is where that shows.
* **Give up quietly.** Every caller has already put the same text on the
  clipboard, so a timeout costs one Ctrl+V, never the text itself.
* **A slash command must be submitted; the task text must not be.** A `/rename`
  or `/color` line is dead unless Enter follows it, but the prompt it hands off
  to is meant to sit there for the user to edit — so `deliver` submits every
  command first and pastes the prompt last. A failed command then costs only
  itself: the prompt still arrives, because it was never made to depend on the
  commands succeeding.

The calling process must not own a console it still needs: attaching to another
console means leaving your own, and there is no way back. A GUI app under
`pythonw.exe` has no console at all and is the easy case; anything with one
loses it on the first attach, so write whatever your own stdout owes the
caller *before* calling in here.
"""

import ctypes
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes
from dataclasses import dataclass

from . import journal

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

PASTE_START = "\x1b[200~"
PASTE_END = "\x1b[201~"

# Ctrl+U — the one keystroke measured to empty the prompt box. See `clear`.
CLEAR_LINE = "\x15"

# Text that only the interactive prompt box draws, so seeing it means the
# session is past its startup dialogs and is reading keystrokes as a prompt.
# Matching on rendered text is a guess about someone else's UI, so it is a
# guess that fails safe: if these ever stop appearing, the paste times out and
# the clipboard copy is still there.
READY_MARKERS = ("shift+tab to cycle", "for shortcuts")

# How a row that carries typed text opens. ">" is the classic bordered box;
# "❯" (U+276F) is the fullscreen TUI, added 2026-08-01 after a live session
# read back `'❯\xa0…'` — see `prompt_box`.
BOX_MARKERS = (">", "❯")

# Three minutes, and the length is the point. This was 45 s, which read as
# generous and was in fact a data-loss deadline: a session is "not ready" for
# every second a startup dialog is on screen, and the workspace-trust question
# a never-opened folder starts on waits for a person (measured 2026-07-26 —
# `is_ready` stayed False for the whole time the dialog was up). At 45 s the
# hand-off gave up for good and the prompt was never typed at all, which is
# exactly the reported failure: "the window opened slowly and my prompt was
# eaten". Waiting costs a poll every 0.4 s on a daemon thread; giving up early
# costs the text.
READY_TIMEOUT = 180.0
POLL_SECONDS = 0.4

# A command's prompt box is already on screen by the time it is sent — unlike
# the first wait, which is for the process to boot — so it gets a much
# shorter timeout.
COMMAND_TIMEOUT = 5.0

# How long to wait for the session to *show* that it took a write: the pasted
# line appearing in the prompt box, then Enter clearing it again. A session
# that is reading at all does both within a frame, so this is a "something is
# wrong, fall back to the clipboard" bound rather than a normal wait.
ECHO_TIMEOUT = 8.0
ECHO_POLL = 0.1

# How many times a prompt that did not visibly land is written again. The text
# appeared within ~220 ms in every measurement, so an attempt that reaches
# ECHO_TIMEOUT is a session that ate it rather than a slow one, and the answer
# to being eaten is to feed it again.
PASTE_ATTEMPTS = 3

# The same, for a command — and fewer, because a command is decoration while
# the prompt is the payload. Every extra attempt puts another ECHO_TIMEOUT
# between the window opening and the tasks arriving in it, and `deliver`
# abandons the remaining commands but pastes the prompt regardless. Two buys
# the retry that matters (see `_write_until_shown`) at 8 s of worst case
# instead of 16.
COMMAND_ATTEMPTS = 2

# What the box shows instead of the text when a paste is long enough:
# "[Pasted text #1 +29 lines]" (measured 2026-07-26 with a 30-line prompt; a
# 3-line one still showed its first line). The number climbs with each paste
# in a session, so only the stem is matched.
PASTED_BLOCK = "[Pasted text"

# Nothing here dresses the console any more, and both halves of that are
# measured rather than dropped.
#
# **The font.** A session used to be forced onto Cascadia Mono, because conhost
# renders the quadrant blocks Claude Code's logo is drawn from (U+2596–259F) as
# boxes in its default Consolas. Windows Terminal draws them, and everything
# else, without help — and it ignores `SetCurrentConsoleFontEx` outright
# (measured 2026-07-26: `_apply_face` returned False against a WT-hosted
# console; the font is a per-profile setting there). Forcing a face is now both
# impossible and unnecessary.
#
# **The icon.** A session's window used to be given `claude.exe`'s icon, so its
# taskbar button said Claude rather than "some terminal". Under WT that cannot
# work and the failure is *silent*, which is the part worth writing down:
# `GetConsoleWindow()` answers a `PseudoConsoleWindow` owned by the client, not
# the visible `CASCADIA_HOSTING_WINDOW_CLASS` window that owns the taskbar
# button — so `WM_SETICON` succeeds against a window nobody sees. Sending it to
# the real Terminal window instead does change that window's icon (measured:
# the handles read back as claude.exe's exactly) and the taskbar still draws
# Terminal's, because a packaged app's button follows its AUMID manifest.
# Confirmed by eye, since no API reads a taskbar button back.
#
# The need it served — telling Claude windows apart — is met by the session's
# title, which `SetConsoleTitleW` is measured to push through to the WT window.

# Enough of a command line to recognise it by in the prompt box. Distinctive
# enough not to match the box's own placeholder hint, and short enough to
# survive a long line wrapping — only the first row of the box carries the
# "> ", so only the first row is ever read back.
ECHO_MATCH = 16

_ERROR_ACCESS_DENIED = 5  # "already attached to a console" from AttachConsole
_KEY_EVENT = 0x0001
_GENERIC_READ = 0x80000000
_GENERIC_WRITE = 0x40000000
_FILE_SHARE_READ_WRITE = 0x00000003
_OPEN_EXISTING = 3
_INVALID_HANDLE = wintypes.HANDLE(-1).value

# Console attachment is per *process*, not per thread, so two hand-offs
# starting at once would fight over which console this process is attached to.
_attachment = threading.Lock()


class _KeyChar(ctypes.Union):
    # Code overlays the same two bytes as UnicodeChar. Writing through it is
    # what lets a surrogate be set at all: half a surrogate pair is not a
    # character, so assigning one to a WCHAR raises TypeError.
    _fields_ = [("UnicodeChar", wintypes.WCHAR), ("AsciiChar", ctypes.c_char),
                ("Code", ctypes.c_ushort)]


class KEY_EVENT_RECORD(ctypes.Structure):
    _fields_ = [
        ("bKeyDown", wintypes.BOOL),
        ("wRepeatCount", wintypes.WORD),
        ("wVirtualKeyCode", wintypes.WORD),
        ("wVirtualScanCode", wintypes.WORD),
        ("uChar", _KeyChar),
        ("dwControlKeyState", wintypes.DWORD),
    ]


class _EventUnion(ctypes.Union):
    # The union is as wide as its largest member (MOUSE_EVENT_RECORD, 16
    # bytes); padding it keeps INPUT_RECORD the size Windows expects even
    # though only key events are ever written.
    _fields_ = [("KeyEvent", KEY_EVENT_RECORD), ("_padding", ctypes.c_byte * 16)]


class INPUT_RECORD(ctypes.Structure):
    _fields_ = [("EventType", wintypes.WORD), ("Event", _EventUnion)]


class COORD(ctypes.Structure):
    _fields_ = [("X", ctypes.c_short), ("Y", ctypes.c_short)]


class SMALL_RECT(ctypes.Structure):
    _fields_ = [("Left", ctypes.c_short), ("Top", ctypes.c_short),
                ("Right", ctypes.c_short), ("Bottom", ctypes.c_short)]


class CONSOLE_SCREEN_BUFFER_INFO(ctypes.Structure):
    _fields_ = [("dwSize", COORD), ("dwCursorPosition", COORD),
                ("wAttributes", wintypes.WORD), ("srWindow", SMALL_RECT),
                ("dwMaximumWindowSize", COORD)]


kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.AttachConsole.argtypes = [wintypes.DWORD]

def utf16_code_units(text: str) -> list[int]:
    """The text as Windows counts it: UTF-16 code units, not Python characters.

    A console input record holds one code unit. Anything outside the BMP — an
    emoji, most obviously — is a surrogate pair and therefore two records, so
    iterating Python characters is wrong for exactly the text a task body is
    most likely to contain.
    """
    raw = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(raw[at:at + 2], "little")
            for at in range(0, len(raw), 2)]


def key_records(text: str) -> ctypes.Array:
    """One key-down/key-up pair per UTF-16 code unit, as a real keypress arrives.

    Only the character matters — Claude reads the console as a stream, not as
    scan codes — so the virtual key and scan code are left at zero.

    Written through uChar.Code rather than uChar.UnicodeChar: a lone surrogate
    is not a character and assigning one to a WCHAR raises TypeError. That
    exception had nowhere to surface, since paste() runs on a daemon thread —
    a single emoji in a task body silently stopped the whole hand-off from
    being typed, leaving only the clipboard copy.
    """
    units = utf16_code_units(text)
    records = (INPUT_RECORD * (len(units) * 2))()
    for index, unit in enumerate(units):
        for offset, pressed in ((0, 1), (1, 0)):
            key = records[index * 2 + offset].Event.KeyEvent
            records[index * 2 + offset].EventType = _KEY_EVENT
            key.bKeyDown = pressed
            key.wRepeatCount = 1
            key.uChar.Code = unit
    return records


@contextmanager
def _attached(pid: int):
    """Borrow another process's console for the body of the `with`."""
    with _attachment:
        if not kernel32.AttachConsole(pid):
            # Access denied means this process still holds a console of its
            # own. Dropping it is the only way to attach elsewhere.
            if ctypes.get_last_error() != _ERROR_ACCESS_DENIED:
                yield False
                return
            kernel32.FreeConsole()
            if not kernel32.AttachConsole(pid):
                yield False
                return
        try:
            yield True
        finally:
            kernel32.FreeConsole()


def _records_for(text: str) -> int:
    """How many input records a full write of `text` is — two per code unit."""
    return len(utf16_code_units(text)) * 2


def _write_input(text: str) -> int:
    """How many of this text's input records actually reached the buffer.

    A count rather than a bool because a SHORT write is not the same failure as
    no write at all: half a bracketed paste leaves the receiving session inside
    a bracket that never closes, and only the count says whether that happened.
    See _write_bracketed, which is the one caller that can act on it.

    Zero for every failure, including a write that reported failure after
    partially succeeding — WriteConsoleInputW's count is not meaningful once it
    has returned false, and acting on a number that might be invented is worse
    than treating the write as having done nothing.
    """
    handle = kernel32.CreateFileW(
        "CONIN$", _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ_WRITE,
        None, _OPEN_EXISTING, 0, None)
    if handle == _INVALID_HANDLE:
        return 0
    records = key_records(text)
    written = wintypes.DWORD(0)
    ok = kernel32.WriteConsoleInputW(handle, records, len(records),
                                     ctypes.byref(written))
    kernel32.CloseHandle(handle)
    return written.value if ok else 0


def _screen_text() -> str:
    """Whatever the attached console is currently showing, as plain text."""
    handle = kernel32.CreateFileW(
        "CONOUT$", _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ_WRITE,
        None, _OPEN_EXISTING, 0, None)
    if handle == _INVALID_HANDLE:
        return ""
    info = CONSOLE_SCREEN_BUFFER_INFO()
    if not kernel32.GetConsoleScreenBufferInfo(handle, ctypes.byref(info)):
        kernel32.CloseHandle(handle)
        return ""
    row_buffer = ctypes.create_unicode_buffer(info.dwSize.X)
    read = wintypes.DWORD(0)
    rows = []
    for row in range(info.srWindow.Top, info.srWindow.Bottom + 1):
        kernel32.ReadConsoleOutputCharacterW(
            handle, row_buffer, info.dwSize.X, COORD(0, row),
            ctypes.byref(read))
        rows.append(row_buffer[:read.value])
    kernel32.CloseHandle(handle)
    return "\n".join(rows)


def is_ready(screen: str) -> bool:
    return any(marker in screen for marker in READY_MARKERS)


def prompt_box(screen: str) -> str:
    """What is currently typed in the session's prompt box.

    The box is drawn below the transcript and above the status line, and its
    first row is the last row on screen that opens with a prompt marker —
    every earlier one is a message already sent, and everything below it is
    indented status. A long entry wraps onto rows that carry no marker, so
    this reads the start of what is typed, which is all any caller needs to
    recognise its own line.

    Reading the box is how a write is *confirmed*: text that has reached it has
    been consumed from the console input buffer, which is the only evidence
    that the next write will be read as a separate event rather than folded
    into this one. Like `is_ready`, this is a guess about someone else's UI, so
    it fails the same safe way — an unrecognised layout reads as an empty box,
    every wait times out, and the clipboard copy is still there.

    **The marker is not only ">".** Claude Code's fullscreen TUI
    (`"tui": "fullscreen"`) draws both sent messages and the input box with
    "❯" and a non-breaking space, inside horizontal rules rather than a
    bordered panel. Measured 2026-08-01 against a live 2.1.220 session: the box
    row read `'❯\\xa0ZZPROBEZZ'` while this function returned `''`, so every
    `submit` and every `paste` into a fullscreen window would have failed its
    own confirmation and been retried to death — silently, because failing
    safe here means falling back to the clipboard. Matching both markers costs
    nothing and keeps the classic layout working exactly as before.
    """
    for row in reversed(screen.split("\n")):
        for marker in BOX_MARKERS:
            if row.startswith(marker):
                return row[len(marker):].strip()
    return ""


def box_shows(screen: str, text: str) -> bool:
    """Whether the prompt box is showing `text` — however it chose to draw it.

    This is the proof that a paste was *taken*, as opposed to written into a
    buffer and dropped, and every part of it is measured rather than assumed:

    * A long paste is not drawn as its text at all. It collapses to
      `[Pasted text #1 +29 lines]`, so matching the prose would report a
      perfectly good hand-off as lost — and then paste it again on top of
      itself.
    * A short paste is drawn as its first line, which may wrap; only the first
      row of the box carries the "> ", so only the start of the line can be
      compared. ECHO_MATCH is the same slice `submit` recognises its own
      command by.
    * An *empty* box is not empty on a fresh session: it holds a placeholder
      hint ('Try "create a util logging.py that..."'). So "the box has
      something in it" proves nothing, and this has to match our own text.

    Fails the same safe way as `is_ready` and `prompt_box`: a layout it cannot
    read reads as "not landed", which costs a retry and then the clipboard,
    never a wrong claim that the text arrived.
    """
    box = prompt_box(screen)
    if not box:
        return False
    if PASTED_BLOCK in box:
        return True
    opening = text.strip().split("\n", 1)[0][:ECHO_MATCH]
    return bool(opening) and opening in box


def console_window(pid: int) -> int:
    """The window handle of the session's console, or 0 if there is not one yet.

    Exposed because the console is this module's to reach — `launcher` needs
    the handle to tell its own window apart from any other that might take the
    foreground, and attaching is the only way to ask for it.
    """
    with _attached(pid) as attached:
        return kernel32.GetConsoleWindow() if attached else 0


def _write(pid: int, text: str) -> bool:
    with _attached(pid) as attached:
        return bool(attached) and _write_input(text) == _records_for(text)


def _write_bracketed(pid: int, text: str) -> bool:
    """Write `text` as one bracketed paste, and never leave the bracket open.

    A short write is the case this exists for. The opening ESC[200~ goes in
    first, so a write that stops partway through has already put the receiving
    session into paste mode with nothing coming to take it out again — and a
    session stuck there reads everything typed next as more pasted content,
    including whatever the user types at the keyboard afterwards. The write has
    failed either way; this is what makes it a failure that ends.

    The closing escape is only sent when the opening one is known to have
    landed in full. A stray ESC[201~ at a prompt that is not inside a paste is
    its own small mess, and not worth risking for a write that may have put
    nothing in the buffer at all.
    """
    payload = PASTE_START + text + PASTE_END
    with _attached(pid) as attached:
        if not attached:
            return False
        written = _write_input(payload)
        if written == _records_for(payload):
            return True
        if written >= _records_for(PASTE_START):
            _write_input(PASTE_END)
        return False


def _wait(pid: int, settled, timeout: float, poll: float = POLL_SECONDS) -> bool:
    """Poll the session's screen until `settled(screen)`, or give up."""
    deadline = time.monotonic() + timeout
    while True:
        with _attached(pid) as attached:
            if attached and settled(_screen_text()):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _record_screen(pid: int, label: str) -> None:
    """Put the session's screen in the log, indented, for a failure to explain.

    This is the artifact that names a cause — a startup dialog, a box that
    already had something in it, a layout nothing here could read. Without it
    a log line only says which step gave up, which is where the last
    investigation started rather than where it ended.
    """
    with _attached(pid) as attached:
        screen = _screen_text() if attached else None
    if screen is None:
        journal.record(f"pid {pid}: {label} — could not attach to the console")
        return
    rows = [row.rstrip() for row in screen.split("\n") if row.strip()]
    journal.record(f"pid {pid}: {label}\n"
                   + "\n".join("    | " + row for row in rows))


def _write_until_shown(pid: int, text: str, shown, attempts: int,
                       what: str) -> bool:
    """Write `text` as a bracketed paste until the session's box shows it.

    The shared half of `paste` and `submit`, and it is shared because the
    failure it answers belongs to neither of them: **a session that is still
    starting takes a write into its console input buffer and never draws it.**
    `_write_bracketed` returns True — the records really did land — so nothing
    but reading the box back can tell that apart from a hand-off that worked.

    This was hardened for the prompt on 2026-07-26 and left unhardened for
    commands, which is the whole of "the colour effect sometimes doesn't
    trigger": one write, one 8 s wait, give up. `delivery.log` 2026-07-27
    06:14:44Z is that shape — `command did not submit: '/color purple'` after
    9.7 s, which is `submit`'s ECHO_TIMEOUT plus the wait for a prompt box.

    Measured against a real, windowless session (2026-07-27) — and the reason
    a retry is the right answer rather than a longer wait:

        is_ready after 1.6s -> True
        prompt_box() -> 'Try "write a test for <filepath>"'   <- placeholder
        ...two seconds later...
        prompt_box() -> ''

    `is_ready` goes true while the box still holds its startup placeholder and
    the layout is still changing. There is no constant that fixes that — the
    session is ready when it is ready — so the writer's move is to notice it
    was not read and feed it again. When the same session was written to after
    it had settled, the command appeared in the box within 0.25 s and Enter
    cleared it within another 0.25 s.

    Two measurements make the retry safe, and it is unsafe without them:

    * **Ctrl+U takes a paste back out**, a collapsed `[Pasted text #1 …]`
      block as well as a typed line. So an attempt that may have half-landed
      can be undone before the next one.
    * **Two pastes with nothing between them concatenate** — measured, the box
      read `…hand-offBUG: a single line hand-off`. So a retry that does not
      clear first turns one lost write into one mangled one, which is worse.

    The last attempt is deliberately *not* cleared. If the confirmation is
    wrong rather than the write — a layout `prompt_box` cannot read — then the
    text is sitting in the box unseen, and clearing it on the way out would
    turn a delivery that actually worked into an empty box. Clearing between
    attempts prevents doubling; not clearing after the last one keeps the
    failure no worse than it used to be. A caller that needs the box empty
    after a failure says so itself: `deliver` calls `clear`.
    """
    for attempt in range(1, attempts + 1):
        wrote = _write_bracketed(pid, text)
        if wrote and _wait(pid, shown, ECHO_TIMEOUT, ECHO_POLL):
            if attempt > 1:
                journal.record(f"pid {pid}: {what} landed on attempt {attempt}")
            return True
        journal.record(f"pid {pid}: {what} attempt {attempt}/{attempts} did not "
                       f"reach the box (write={'ok' if wrote else 'failed'})")
        if attempt == attempts:
            return False
        # Whatever did arrive has to come out before writing it again.
        _write(pid, CLEAR_LINE)
    return False


def paste(pid: int, text: str, timeout: float = READY_TIMEOUT,
          attempts: int = PASTE_ATTEMPTS) -> bool:
    """Type `text` into the process's prompt box, and prove that it arrived.

    This used to be write-and-hope, and it was the only write in the whole
    protocol that was never checked: a command is proven twice — seen in the
    box, then seen to leave it — while the prompt, the thing the hand-off
    exists to deliver, was written into a buffer and forgotten. So every way
    it could be lost was silent, unrecoverable and unrecorded, which is how
    "sometimes the prompt gets eaten" stayed a mystery long enough to be
    reported as a feeling about slow windows.

    The retry itself, and the measurements that make it safe, are
    `_write_until_shown` — which `submit` now shares, because a command was
    losable exactly the same way and only the prompt had been hardened.
    """
    if not text:
        return False
    if not _wait(pid, is_ready, timeout):
        journal.record(f"pid {pid}: paste gave up waiting for a prompt box "
                       f"after {timeout:.0f}s")
        _record_screen(pid, "screen when the wait for a prompt box ran out")
        return False
    if _write_until_shown(pid, text, lambda screen: box_shows(screen, text),
                          attempts, "prompt"):
        return True
    _record_screen(pid, "screen after the last prompt attempt")
    return False


def submit(pid: int, line: str, timeout: float = COMMAND_TIMEOUT,
           attempts: int = COMMAND_ATTEMPTS) -> bool:
    """Type `line` into the process's prompt box and press Enter for it.

    Bracketed for the same reason `paste` is bracketed, but for the opposite
    danger: a leading "/" opens Claude Code's command-suggestion popup, a live
    UI that reads keystrokes as they arrive. A raw Enter sent right after raw
    characters risks the popup reading it as "accept the highlighted
    suggestion" rather than "submit what I typed". A paste arrives as one
    event carrying the whole line, so the popup never sees a partial token and
    has nothing to select — only then is the Enter, a separate write, safe to
    send.

    The Enter is not sent until the line is *visible in the prompt box*, and
    the call is not finished until it has left again. Both waits are the fix
    for one measured failure: written back to back, the line and its Enter
    queue in the console input buffer, and a session that reads them in a
    single pass treats a `\\r` sitting between two bracketed pastes as part of
    the paste. The commands then merge onto one line — `/rename …/color green`
    — with both Enters silently discarded, and the task prose lands on the end
    of that same line. Seeing the box change is the only proof the session
    read one write before the next one arrives; a `time.sleep` is not, because
    it measures the writer rather than the reader.

    An empty line is nothing to submit: writing empty paste markers and then
    pressing Enter would send a blank prompt to the session. `setup_commands`
    never produces one, but this and `paste` are read as a pair and refuse an
    empty argument the same way.

    **And they are a pair in the retry too, which they were not until
    2026-07-27.** A command written into a session that has not started
    reading is dropped exactly as a prompt is, and this gave up after one
    attempt while `paste` tried three — so "the colour effect sometimes
    doesn't trigger" was the already-diagnosed defect surviving in the half
    nobody had measured. See `_write_until_shown`. `COMMAND_ATTEMPTS` is
    smaller than `PASTE_ATTEMPTS` because the prompt is the payload and a
    command is decoration on it.
    """
    if not line:
        return False
    if not _wait(pid, is_ready, timeout):
        return False
    echo = line[:ECHO_MATCH]
    if not _write_until_shown(pid, line,
                              lambda screen: echo in prompt_box(screen),
                              attempts, f"command {line!r}"):
        _record_screen(pid, f"screen after the last attempt at {line!r}")
        return False
    if not _write(pid, "\r"):
        return False
    return _wait(pid, lambda screen: echo not in prompt_box(screen),
                 ECHO_TIMEOUT, ECHO_POLL)


def clear(pid: int, line: str) -> bool:
    """Take a command that was typed but never submitted back out of the box.

    A failed `submit` leaves its line sitting in the prompt box, and the
    prompt is pasted regardless — so without this the task prose would be
    appended to a half-typed `/rename` and handed over as one line, which is
    the very thing invariant 2 promises cannot happen to a body.

    Ctrl+U, measured against a live session rather than assumed. Escape was
    the obvious guess and does nothing at all to a typed line; Ctrl+C clears
    it but exits the session on a second press, and this runs on a path where
    something has already gone wrong.

    **Its success is weak evidence, and that is a known limit rather than a
    bug.** The proof it uses is the echo *leaving* `prompt_box`, which an echo
    that was never visible there satisfies on the first poll — so "cleared"
    and "there was nothing to clear" are the same answer.

    That was written up as a likely glue-the-prompt-onto-a-leftover-command
    defect on 2026-07-27, and instrumenting a real windowless session the same
    day argued against it: `prompt_box` reads the real layout correctly
    (`'>\\xa0/color purple'` → `'/color purple'`, with the separator a
    non-breaking space that `strip()` removes), the command appears within
    0.25 s, and Enter clears it within another 0.25 s. So a command `submit`
    could not see is most likely a command the session never took — the
    startup drop `_write_until_shown` now retries — and there is then nothing
    in the box for the prompt to land on top of.

    What is genuinely unproven is the *other* branch: a box holding text this
    module did not put there. Ctrl+U is written unconditionally, so that case
    is handled by luck rather than by design. Settling it needs a session that
    reproduces the failure, and this one would not.
    """
    if not _write(pid, CLEAR_LINE):
        return False
    echo = line[:ECHO_MATCH]
    return _wait(pid, lambda screen: echo not in prompt_box(screen),
                 ECHO_TIMEOUT, ECHO_POLL)


@dataclass(frozen=True)
class Delivery:
    """What a delivery managed to do. Reported, never raised.

    "Give up quietly" is still the rule for the *session* — nothing here
    throws, and the caller's clipboard copy is still the fallback — but quiet
    used to mean nobody was told at all, so a hand-off that typed nothing
    looked exactly like one that worked until someone glanced at the window.
    A consumer that knows can say the one useful sentence: it is on your
    clipboard, press Ctrl+V.
    """

    commands_submitted: int
    commands_total: int
    had_prompt: bool
    prompt_typed: bool
    seconds: float

    @property
    def complete(self) -> bool:
        """Everything asked for reached the session."""
        return (self.commands_submitted == self.commands_total
                and self.prompt_typed == self.had_prompt)


def deliver(pid: int, commands: list[str], prompt: str) -> Delivery:
    """Submit every command, then leave `prompt` typed but unsent.

    Commands go first because they are decoration on the hand-off, not the
    hand-off itself: a command that fails to submit abandons the commands
    still queued behind it, but never costs the prompt, which is pasted
    regardless. Only the first wait is for a process that has not booted yet
    — every command after it is typed into a prompt box already on screen, so
    it gets the shorter `COMMAND_TIMEOUT` instead of `READY_TIMEOUT`.

    A command that failed is cleared before the prompt is pasted. "Failed"
    here usually means "typed but not submitted", so its text is still in the
    box; pasting on top of it would hand over the task prose glued to half a
    slash command.

    This used to dress the console first — an icon and a font, applied before
    the wait because they needed only the console to exist, not the session to
    be up. Both are gone; see the note beside the constants for what Windows
    Terminal does with each. So the first thing that happens is the long wait
    for a prompt box.

    Returns a `Delivery` rather than nothing. It still runs on a daemon thread
    with nobody waiting on it, so the return is for `deliver_when_ready`'s
    callback and for the log — a caller that ignores it is in exactly the
    position every caller used to be in.
    """
    began = time.monotonic()
    journal.record(f"pid {pid}: delivering {len(commands)} command(s) and "
                   f"{len(prompt.splitlines())} prompt line(s)")
    submitted = 0
    timeout = READY_TIMEOUT
    for command in commands:
        if not submit(pid, command, timeout=timeout):
            journal.record(f"pid {pid}: command did not submit: {command!r}")
            clear(pid, command)
            break
        submitted += 1
        timeout = COMMAND_TIMEOUT
    typed = bool(prompt) and paste(pid, prompt)
    result = Delivery(commands_submitted=submitted, commands_total=len(commands),
                      had_prompt=bool(prompt), prompt_typed=typed,
                      seconds=time.monotonic() - began)
    # ASCII in the summary line on purpose: the screen dumps below it are full
    # of box drawing and cannot be, but the line a person skims should survive
    # a `Get-Content` at this machine's cp1252 console codepage.
    journal.record(
        f"pid {pid}: {'delivered' if result.complete else 'INCOMPLETE'} - "
        f"commands {submitted}/{len(commands)}, "
        f"prompt {'typed' if typed else 'not typed' if prompt else 'none'}, "
        f"{result.seconds:.1f}s")
    return result


def deliver_when_ready(pid: int, commands: list[str], prompt: str,
                       on_finish=None) -> threading.Thread:
    """Start `deliver` in the background so the caller's UI never waits on it.

    `on_finish` is called with the `Delivery` when it is over, on that same
    background thread. A consumer that wants to tell someone the text never
    arrived has no other way to find out: by then the call that started this
    returned minutes ago.

    A callback that raises is logged and swallowed. It runs on a daemon thread
    where an exception has nowhere to surface, and a consumer's reporting bug
    must not become a delivery that half-happened.
    """
    def run():
        result = deliver(pid, commands, prompt)
        if on_finish is None:
            return
        try:
            on_finish(result)
        except Exception as error:
            journal.record(f"pid {pid}: on_finish raised {error!r}")

    waiter = threading.Thread(target=run, daemon=True)
    waiter.start()
    return waiter
