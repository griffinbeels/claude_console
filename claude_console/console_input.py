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
import shutil
import threading
import time
from contextlib import contextmanager
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
user32 = ctypes.WinDLL("user32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)

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

READY_TIMEOUT = 45.0
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

# The face a handed-off session is rendered in, and how long to keep trying
# to apply it — the console does not exist for the first moments after Popen
# returns, so the attach fails until it does.
#
# Consolas is what a console falls back to with nothing configured, and it has
# no quadrant block elements (U+2596–259F) — the characters Claude Code's logo
# is drawn from. conhost does not fall back for them (it does font-link ⎿, ⏵,
# ⏺, ✓, ✗ and ✻, which is why only the logo is affected), so they render as a
# row of boxes with the code point printed inside. Cascadia Mono ships with
# Windows 11 and has all of them.
SESSION_FACE = "Cascadia Mono"
FONT_TIMEOUT = 10.0

# FF_MODERN | TMPF_VECTOR | TMPF_TRUETYPE — what a console stores for a
# TrueType face, against 0 for the raster default.
_TRUETYPE_FAMILY = 54
_NORMAL_WEIGHT = 400

# The image a session's icon is taken from, and how long to keep trying to put
# it on the window — the same wait the font needs, for the same reason: the
# console does not exist for the first moments after Popen returns.
#
# Resolved on PATH rather than taken from the launch command, because a
# per-project override like `pwsh -c claude` still opens a Claude session and
# should still say so on the taskbar.
SESSION_IMAGE = "claude"
ICON_TIMEOUT = 10.0

# ICON_BIG is what the taskbar and Alt+Tab draw; ICON_SMALL is the title bar.
# Setting one leaves the other on conhost's class icon.
WM_SETICON = 0x0080
ICON_SMALL, ICON_BIG = 0, 1

# A cross-process SendMessage blocks until the receiving process pumps its
# message queue, and this runs on the hand-off's daemon thread. SMTO_ABORTIFHUNG
# plus a bound means a wedged console costs the icon rather than the thread.
_SMTO_ABORTIFHUNG = 0x0002
_ICON_SEND_MS = 1000

# The one pair of handles every session's window is given. See session_icons.
_icons = None

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


class CONSOLE_FONT_INFOEX(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.ULONG), ("nFont", wintypes.DWORD),
                ("dwFontSize", COORD), ("FontFamily", wintypes.UINT),
                ("FontWeight", wintypes.UINT),
                ("FaceName", wintypes.WCHAR * 32)]  # LF_FACESIZE


kernel32.CreateFileW.restype = wintypes.HANDLE
kernel32.AttachConsole.argtypes = [wintypes.DWORD]
shell32.ExtractIconExW.argtypes = [wintypes.LPCWSTR, ctypes.c_int,
                                   ctypes.POINTER(wintypes.HICON),
                                   ctypes.POINTER(wintypes.HICON), wintypes.UINT]
user32.SendMessageTimeoutW.argtypes = [wintypes.HWND, wintypes.UINT,
                                       wintypes.WPARAM, wintypes.LPARAM,
                                       wintypes.UINT, wintypes.UINT,
                                       ctypes.POINTER(wintypes.WPARAM)]


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


def _apply_face(face: str) -> bool:
    """Put the attached console on `face`, and undo it if that is not what took.

    An unknown face is not refused: the console accepts the call and picks
    something of its own, which on a machine without this font would be a
    downgrade rather than a fix. So the face is read back, and anything other
    than the one asked for is put straight back to what it was.

    Only the face is chosen — the size comes from whatever the console already
    had, so a user who set a size keeps it.
    """
    handle = kernel32.CreateFileW(
        "CONOUT$", _GENERIC_READ | _GENERIC_WRITE, _FILE_SHARE_READ_WRITE,
        None, _OPEN_EXISTING, 0, None)
    if handle == _INVALID_HANDLE:
        return False
    try:
        before = CONSOLE_FONT_INFOEX()
        before.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        if not kernel32.GetCurrentConsoleFontEx(handle, False,
                                                ctypes.byref(before)):
            return False
        wanted = CONSOLE_FONT_INFOEX.from_buffer_copy(before)
        wanted.FaceName = face
        wanted.FontFamily = _TRUETYPE_FAMILY
        wanted.FontWeight = _NORMAL_WEIGHT
        if not kernel32.SetCurrentConsoleFontEx(handle, False,
                                                ctypes.byref(wanted)):
            return False
        landed = CONSOLE_FONT_INFOEX()
        landed.cbSize = ctypes.sizeof(CONSOLE_FONT_INFOEX)
        kernel32.GetCurrentConsoleFontEx(handle, False, ctypes.byref(landed))
        if landed.FaceName == face:
            return True
        kernel32.SetCurrentConsoleFontEx(handle, False, ctypes.byref(before))
        return False
    finally:
        kernel32.CloseHandle(handle)


def use_font(pid: int, face: str = SESSION_FACE,
             timeout: float = FONT_TIMEOUT) -> bool:
    """Render the spawned session's console in a face that has its glyphs.

    Worth doing at all only because of which host draws the window. Windows
    delegates every new console to the default terminal application, and when
    that is Windows Terminal the glyphs are fine — WT falls back to another
    font per glyph. When it is Windows Console Host, as on this machine, there
    is no fallback for the block elements and the logo comes out as boxes.
    Conhost is also the only one of the two that honours `SW_SHOWNOACTIVATE`,
    so it is the host worth keeping (invariant 10) and the font is the part
    worth changing.

    Setting it after the logo has already been painted is fine: the console
    repaints its whole buffer in the new face. Retrying is not for that, it is
    for the moment after `Popen` returns when the console does not exist yet.
    """
    deadline = time.monotonic() + timeout
    while True:
        with _attached(pid) as attached:
            if attached and _apply_face(face):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def session_icons() -> tuple[int, int]:
    """The large and small icons a session's window wears, extracted once.

    One pair serves every session, and **nothing ever destroys them**:
    `DestroyIcon` on a handle a live window is holding is how an icon goes
    blank, and every open session is holding this one. Extraction is cheap
    enough to make the cache about correctness rather than speed — measured at
    0.000 s against a 265 MB `claude.exe`.

    `(0, 0)` when `claude` is not on PATH, or when the image it names carries
    no icons. That is a session without an icon, not a session that failed.
    """
    global _icons
    if _icons is None:
        image = shutil.which(SESSION_IMAGE)
        large, small = wintypes.HICON(), wintypes.HICON()
        if image:
            shell32.ExtractIconExW(image, 0, ctypes.byref(large),
                                   ctypes.byref(small), 1)
        _icons = (large.value or 0, small.value or 0)
    return _icons


def _apply_icon(icons: tuple[int, int]) -> bool:
    """Put `icons` on the attached console's window, or say it could not.

    Reads the window handle directly rather than through `console_window`,
    which takes the attachment lock this is already holding.

    A window handle of 0 is the ordinary case of being early — the console
    exists before its window does — and also the permanent case for a console
    opened with CREATE_NO_WINDOW, which has no window at all (measured:
    `GetConsoleWindow` answers 0 for one). Neither is an error; both are a
    console this cannot dress.
    """
    window = kernel32.GetConsoleWindow()
    if not window:
        return False
    for kind, icon in ((ICON_BIG, icons[0]), (ICON_SMALL, icons[1])):
        answer = wintypes.WPARAM(0)
        if not user32.SendMessageTimeoutW(window, WM_SETICON, kind, icon,
                                          _SMTO_ABORTIFHUNG, _ICON_SEND_MS,
                                          ctypes.byref(answer)):
            return False
    return True


def use_icon(pid: int, timeout: float = ICON_TIMEOUT) -> bool:
    """Give the spawned session's window the icon of the thing running in it.

    A console window wears the icon of the image its host was launched as, not
    of the program inside it — so pinning the host (invariant 10) quietly cost
    the session its Anthropic logo and left every hand-off looking like any
    other terminal on the taskbar. Measured 2026-07-26, comparing icons pixel
    by pixel: a window from `conhost.exe claude …` carries conhost.exe's icon
    byte-for-byte, while `claude.exe` has one of its own two resource groups
    away.

    Setting it from here is the cheap half of the fix. The expensive half — a
    shortcut handed to conhost as STARTF_TITLEISLINKNAME, which makes conhost
    load the icon itself — would own the handle for the window's whole life,
    and costs `subprocess.Popen` in the eight lines that carry invariants 8 and
    10. This does not: the handles belong to *this* process, so a session that
    outlives the tracker is left holding a dead one (measured: an icon handle
    goes invalid the moment its process exits, and neither LR_SHARED nor a
    module-resource load changes that).
    """
    icons = session_icons()
    if not all(icons):
        return False
    deadline = time.monotonic() + timeout
    while True:
        with _attached(pid) as attached:
            if attached and _apply_icon(icons):
                return True
        if time.monotonic() >= deadline:
            return False
        time.sleep(POLL_SECONDS)


def is_ready(screen: str) -> bool:
    return any(marker in screen for marker in READY_MARKERS)


def prompt_box(screen: str) -> str:
    """What is currently typed in the session's prompt box.

    The box is drawn below the transcript and above the status line, and its
    first row is the last row on screen that starts with ">" — every earlier
    one is a message already sent, and everything below it is indented status.
    A long entry wraps onto rows that carry no marker, so this reads the start
    of what is typed, which is all any caller needs to recognise its own line.

    Reading the box is how a write is *confirmed*: text that has reached it has
    been consumed from the console input buffer, which is the only evidence
    that the next write will be read as a separate event rather than folded
    into this one. Like `is_ready`, this is a guess about someone else's UI, so
    it fails the same safe way — an unrecognised layout reads as an empty box,
    every wait times out, and the clipboard copy is still there.
    """
    for row in reversed(screen.split("\n")):
        if row.startswith(">"):
            return row[1:].strip()
    return ""


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


def paste(pid: int, text: str, timeout: float = READY_TIMEOUT) -> bool:
    """Type `text` into the process's prompt box once it is accepting input."""
    if not text:
        return False
    if not _wait(pid, is_ready, timeout):
        return False
    return _write_bracketed(pid, text)


def submit(pid: int, line: str, timeout: float = COMMAND_TIMEOUT) -> bool:
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
    """
    if not line:
        return False
    if not _wait(pid, is_ready, timeout):
        return False
    if not _write_bracketed(pid, line):
        return False
    echo = line[:ECHO_MATCH]
    if not _wait(pid, lambda screen: echo in prompt_box(screen),
                 ECHO_TIMEOUT, ECHO_POLL):
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
    """
    if not _write(pid, CLEAR_LINE):
        return False
    echo = line[:ECHO_MATCH]
    return _wait(pid, lambda screen: echo not in prompt_box(screen),
                 ECHO_TIMEOUT, ECHO_POLL)


def deliver(pid: int, commands: list[str], prompt: str) -> None:
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

    The icon and the font go first, before the wait for a prompt box — they
    are the two things here that do not need the session to be up, only its
    console to exist, and a session opened with nothing selected gets both.
    The icon leads: the taskbar button is there from the moment the window is,
    and it is what tells this window apart from every other console.

    Returns nothing: this runs off the caller's thread, on a daemon thread
    with no one left to hand a result to.
    """
    # Passed rather than defaulted, like every other timeout here: a default
    # is bound at import, so the module attribute is the only spelling a test
    # can shorten.
    use_icon(pid, timeout=ICON_TIMEOUT)
    use_font(pid, timeout=FONT_TIMEOUT)
    timeout = READY_TIMEOUT
    for command in commands:
        if not submit(pid, command, timeout=timeout):
            clear(pid, command)
            break
        timeout = COMMAND_TIMEOUT
    if prompt:
        paste(pid, prompt)


def deliver_when_ready(pid: int, commands: list[str], prompt: str) -> threading.Thread:
    """Start `deliver` in the background so the caller's UI never waits on it."""
    waiter = threading.Thread(target=deliver, args=(pid, commands, prompt),
                              daemon=True)
    waiter.start()
    return waiter
