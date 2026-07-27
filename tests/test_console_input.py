import subprocess
import sys
from pathlib import Path

import pytest

from claude_console import console_input

import _console_probe

PROBE = str(Path(__file__).with_name("_console_probe.py"))


class NoConsole:
    """An attach that fails, the way it does before a session has a console."""

    def __enter__(self):
        return False

    def __exit__(self, *exc):
        return False


@pytest.fixture(autouse=True)
def no_test_reaches_a_real_console(monkeypatch):
    """Nothing in this file may touch a console, and the default is refusal.

    `_attached` frees the calling process's console in order to attach
    elsewhere — and the calling process here is pytest, so a call that slips
    through takes pytest's own console with it. Tests that want a console opt
    in by patching over this.
    """
    monkeypatch.setattr(console_input, "_attached", lambda pid: NoConsole())
    monkeypatch.setattr(console_input, "POLL_SECONDS", 0)


def test_the_probe_console_never_reaches_the_screen():
    # The suite runs constantly while someone else is using the machine, so the
    # one test that opens a real console must open a windowless one. Under a
    # Windows 11 default terminal of Windows Terminal, CREATE_NEW_CONSOLE opens
    # a full Terminal window and the spawner's SW_SHOWNOACTIVATE is discarded —
    # WT creates that window, not us, so there is no flag to soften it with.
    # CREATE_NO_WINDOW is the only spelling that stays off screen.
    assert _console_probe.CONSOLE_FLAGS == subprocess.CREATE_NO_WINDOW


def characters_of(records):
    return "".join(records[index].Event.KeyEvent.uChar.UnicodeChar
                   for index in range(len(records)))


def test_every_character_is_written_as_a_press_and_a_release():
    records = console_input.key_records("hi")
    pressed = [bool(records[index].Event.KeyEvent.bKeyDown)
               for index in range(len(records))]

    assert len(records) == 4
    assert characters_of(records) == "hhii"
    assert pressed == [True, False, True, False]


def code_units_of(records):
    """The raw UTF-16 code unit in each record.

    Read as a number rather than through UnicodeChar: half a surrogate pair is
    not a character, and asking ctypes to hand one back as a `str` is asking
    for trouble that has nothing to do with what is being tested.
    """
    return [records[index].Event.KeyEvent.uChar.Code
            for index in range(len(records))]


def utf16_units(text):
    raw = text.encode("utf-16-le", "surrogatepass")
    return [int.from_bytes(raw[at:at + 2], "little")
            for at in range(0, len(raw), 2)]


def test_an_emoji_is_typed_as_its_two_utf16_code_units():
    # A console input record holds one UTF-16 code unit, not one Python
    # character. An emoji is a surrogate pair, so it needs two records — four
    # with press and release — and assigning it to a single WCHAR raises
    # TypeError. That exception surfaced nowhere: paste() runs on a daemon
    # thread, so one emoji in a task body silently stopped the entire hand-off
    # from being typed, with the clipboard copy the only surviving path.
    records = console_input.key_records("\U0001F680")

    assert len(records) == 4
    assert code_units_of(records) == [0xD83D, 0xD83D, 0xDE80, 0xDE80]


def test_a_body_mixing_plain_text_and_an_emoji_keeps_every_code_unit_in_order():
    text = "ship it \U0001F680 now"

    records = console_input.key_records(text)

    expected = [unit for unit in utf16_units(text) for _ in range(2)]
    assert code_units_of(records) == expected


def test_a_session_is_ready_once_its_prompt_hint_is_on_screen():
    assert console_input.is_ready("  ⏵⏵ bypass permissions on (shift+tab to cycle)")
    assert console_input.is_ready("? for shortcuts")


def test_a_startup_dialog_is_not_a_ready_session():
    # The workspace-trust question a never-opened folder starts on. Typing
    # here would answer it — its default is "Yes, I trust this folder" — and
    # the task text would be swallowed by the dialog instead of reaching the
    # prompt box.
    assert not console_input.is_ready(
        "Quick safety check: Is this a project you created or one you trust?\n"
        "❯ 1. Yes, I trust this folder\n  2. No, exit\n"
        "Enter to confirm · Esc to cancel")


def test_nothing_is_typed_for_empty_text():
    # Guards the no-selection hand-off, which must open a session and leave
    # its prompt alone. A pid of 0 would raise if it ever got as far as
    # attaching to a console.
    assert console_input.paste(0, "", timeout=5) is False


def test_text_is_typed_into_another_process_console():
    """The whole mechanism, against a real console this process does not own.

    The console is real; its window is never shown. See `_console_probe`.
    """
    probe = subprocess.run([sys.executable, PROBE, "parent"],
                           capture_output=True, text=True, timeout=90)

    assert probe.returncode == 0, probe.stderr
    assert "typed=True" in probe.stdout, probe.stdout
    # Delivered whole, and wrapped in the bracketed-paste markers so the
    # receiver treats it as one paste rather than keystrokes ending in Enter.
    assert "delivered=True" in probe.stdout, probe.stdout


def test_deliver_submits_every_command_before_typing_the_prompt(monkeypatch):
    events = []

    def fake_submit(pid, line, timeout=None):
        events.append(("submit", line))
        return True

    monkeypatch.setattr(console_input, "submit", fake_submit)
    monkeypatch.setattr(console_input, "paste",
                        lambda pid, text: events.append(("paste", text)))

    console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    assert events == [("submit", "/rename A"),
                      ("submit", "/color red"),
                      ("paste", "BUG: body")]


def test_a_command_that_fails_does_not_cost_the_prompt(monkeypatch):
    # The commands are decoration; the editable prompt text is the hand-off.
    # `clear` is stubbed along with the rest: unstubbed it would attach to a
    # console, and attaching means leaving pytest's own.
    pasted = {}
    monkeypatch.setattr(console_input, "submit",
                        lambda pid, line, timeout=None: False)
    monkeypatch.setattr(console_input, "clear", lambda pid, line: True)
    monkeypatch.setattr(console_input, "paste",
                        lambda pid, text: pasted.update(text=text))

    console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    assert pasted == {"text": "BUG: body"}


def test_the_first_failed_command_abandons_the_rest(monkeypatch):
    tried = []

    def failing_submit(pid, line, timeout=None):
        tried.append(line)
        return False

    monkeypatch.setattr(console_input, "submit", failing_submit)
    monkeypatch.setattr(console_input, "clear", lambda pid, line: True)
    monkeypatch.setattr(console_input, "paste", lambda pid, text: None)

    console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    assert tried == ["/rename A"]


def test_the_first_command_waits_for_the_session_to_boot(monkeypatch):
    # The first wait is for a process to start; every later one is for a prompt
    # box already on screen.
    waits = []
    monkeypatch.setattr(console_input, "submit",
                        lambda pid, line, timeout=None: waits.append(timeout) or True)
    monkeypatch.setattr(console_input, "paste", lambda pid, text: None)

    console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    assert waits == [console_input.READY_TIMEOUT, console_input.COMMAND_TIMEOUT]


def test_no_commands_is_just_a_paste(monkeypatch):
    pasted = {}
    monkeypatch.setattr(console_input, "paste",
                        lambda pid, text: pasted.update(text=text))

    console_input.deliver(7, [], "BUG: body")

    assert pasted == {"text": "BUG: body"}


def test_an_empty_prompt_is_never_typed(monkeypatch):
    pasted = []
    monkeypatch.setattr(console_input, "submit",
                        lambda pid, line, timeout=None: True)
    monkeypatch.setattr(console_input, "paste",
                        lambda pid, text: pasted.append(text))

    console_input.deliver(7, ["/rename A"], "")

    assert pasted == []


class FakeAttach:
    """A console this process is attached to, without a console existing."""

    def __enter__(self):
        return True

    def __exit__(self, *exc):
        return False


# One real screen, captured from a live session, so the layout `prompt_box`
# reads is the layout Claude Code actually draws rather than one invented here.
LIVE_SCREEN = """
 ▐▛███▜▌   Claude Code v2.1.220
▝▜█████▛▘  Opus 5 with xhigh effort · Claude Max
  ▘▘ ▝▝    ~\\Desktop\\code\\task_tracker

> /rename GAP0
  ⎿  Session renamed to: GAP0

                                            ◉ xhigh · /effort
─────────────────────────────────────────────────────── GAP0 ──
> /color green
────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent
"""


def test_the_prompt_box_is_read_from_below_the_transcript():
    # Both the box and an already-sent message start with ">", and the box is
    # always the lower of the two: read the wrong one and a command looks
    # submitted the moment it is echoed into the transcript.
    assert console_input.prompt_box(LIVE_SCREEN) == "/color green"


def test_an_unrecognisable_screen_reads_as_an_empty_box():
    # The same fail-safe as READY_MARKERS: a layout this does not understand
    # must not look like a session that took the text.
    assert console_input.prompt_box("no prompt here\n  indented > quote") == ""


# The same session with nothing typed in it, captured the same way — the one
# rendering the pacing scheme turns on and the only one the repo had never
# held. `submit` waits for its line to LEAVE the box, and that wait can only
# clear if an empty box still draws a row of its own: with no marker there,
# `prompt_box` would fall back to the transcript's own "> …" and every command
# would time out and be dropped in silence. It does draw one. Filler rows
# between the banner and the status area are dropped, as in LIVE_SCREEN above;
# nothing else is edited.
EMPTY_SCREEN = """
 ▐▛███▜▌   Claude Code v2.1.220
▝▜█████▛▘  Opus 5 with xhigh effort · Claude Max
  ▘▘ ▝▝    ~\\Desktop\\code\\task_tracker

                                                                                                      ◉ xhigh · /effort
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
>
────────────────────────────────────────────────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent                                                       focus
"""


def test_an_empty_prompt_box_still_draws_its_marker_row():
    # Measured, not assumed: every pacing test below runs through FakeSession,
    # whose screen() returns f"> {self.box}" and so emits "> " for an empty
    # box by construction. The fake would look identical if Claude Code drew
    # nothing at all.
    assert console_input.prompt_box(EMPTY_SCREEN) == ""


def test_the_empty_capture_is_a_session_that_is_accepting_input():
    # Same screen, so one capture is evidence for both halves — this is what a
    # session past its startup dialogs looks like with nothing typed.
    assert console_input.is_ready(EMPTY_SCREEN)


class FakeSession:
    """A console that answers writes the way a live session was measured to.

    A bracketed paste lands in the prompt box; Enter and Escape empty it. The
    screen comes back in the real layout, so `prompt_box` is exercised rather
    than stubbed out.

    `reads_input` and `ignores_enter` are the two ways a real session stops
    keeping up — the whole reason the writes have to be paced by what the box
    shows rather than by a sleep.
    """

    def __init__(self, reads_input=True, ignores_enter=False, writes_at_most=None):
        self.box = ""
        self.reads_input = reads_input
        self.ignores_enter = ignores_enter
        # A ceiling on how many input records one write may land, for the
        # short-write case: WriteConsoleInputW is allowed to accept fewer
        # records than it was handed, which cuts a bracketed paste in half.
        self.writes_at_most = writes_at_most
        self.writes = []
        self.boxes = []

    def write(self, text):
        # Stands in for _write_input, so it answers in the same currency:
        # input records landed, two per UTF-16 code unit, NOT a success flag.
        # A short count is how a partial write is expressed.
        landed = console_input._records_for(text)
        if self.writes_at_most is not None:
            landed = min(landed, self.writes_at_most)
        self.writes.append(text)
        self.boxes.append(self.box)
        if not self.reads_input:
            return landed
        if text == console_input.CLEAR_LINE or (text == "\r"
                                                and not self.ignores_enter):
            self.box = ""
        elif text != "\r":
            self.box += text.replace(console_input.PASTE_START, "").replace(
                console_input.PASTE_END, "")
        return landed

    def screen(self):
        return ("> a message already sent\n"
                "──────────────────────────\n"
                f"> {self.box}\n"
                "──────────────────────────\n"
                "  ⏵⏵ bypass permissions on (shift+tab to cycle)")


def live_session(monkeypatch, **behaviour):
    session = FakeSession(**behaviour)
    monkeypatch.setattr(console_input, "ECHO_TIMEOUT", 0.05)
    monkeypatch.setattr(console_input, "ECHO_POLL", 0)
    monkeypatch.setattr(console_input, "_write_input", session.write)
    monkeypatch.setattr(console_input, "_screen_text", session.screen)
    monkeypatch.setattr(console_input, "_attached", lambda pid: FakeAttach())
    return session


def test_a_submitted_line_is_bracketed_and_followed_by_its_own_enter(monkeypatch):
    # Bracketed so the "/" command popup never sees a partial token; the Enter
    # is a separate write so the popup cannot swallow it as a selection.
    session = live_session(monkeypatch)

    assert console_input.submit(7, "/color red") is True
    assert session.writes == [
        console_input.PASTE_START + "/color red" + console_input.PASTE_END,
        "\r",
    ]


def test_the_enter_waits_for_the_line_to_reach_the_prompt_box(monkeypatch):
    """The measured bug: two writes a session reads in one pass are one event.

    Written back to back against a session that had not drained its console
    input buffer, `/rename …` and `/color green` arrived as a single line with
    both Enters discarded — a `\\r` between two bracketed pastes is read as
    part of the paste. So the Enter is not written at all until the line is
    visible in the box, which is the only proof the paste was consumed.
    """
    session = live_session(monkeypatch, reads_input=False)

    assert console_input.submit(7, "/color red") is False
    assert session.writes == [
        console_input.PASTE_START + "/color red" + console_input.PASTE_END]


def test_a_command_is_never_typed_on_top_of_one_still_in_the_box(monkeypatch):
    session = live_session(monkeypatch)

    console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    pastes = [box for text, box in zip(session.writes, session.boxes)
              if text.startswith(console_input.PASTE_START)]
    assert pastes == ["", "", ""]


def test_a_command_that_never_submits_is_cleared_before_the_prompt(monkeypatch):
    # Otherwise the task prose is pasted onto the end of the unsubmitted
    # command and handed over as one line, which breaks invariant 2 on the
    # body without touching the body.
    session = live_session(monkeypatch, ignores_enter=True)

    console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    assert session.writes == [
        console_input.PASTE_START + "/rename A" + console_input.PASTE_END,
        "\r",
        console_input.CLEAR_LINE,
        console_input.PASTE_START + "BUG: body" + console_input.PASTE_END,
    ]

def test_an_empty_line_is_never_submitted(monkeypatch):
    # Without the guard this writes empty paste markers and then presses
    # Enter, submitting a blank prompt to the session — the same reason paste()
    # refuses empty text.
    session = live_session(monkeypatch)

    assert console_input.submit(7, "") is False
    assert session.writes == []


def test_a_paste_cut_short_still_closes_its_bracket(monkeypatch):
    # WriteConsoleInputW may accept fewer records than it is handed. Partway
    # through a bracketed paste, the opening ESC[200~ has landed and the
    # closing one never will — so the session stays in paste mode and reads
    # everything typed next, by this app or by the user at the keyboard, as
    # more pasted content. The write is a failure either way; what must not
    # happen is that it leaves the session wedged.
    opening = console_input._records_for(console_input.PASTE_START)
    session = live_session(monkeypatch, writes_at_most=opening + 4)

    assert console_input.paste(7, "BUG: body") is False
    assert session.writes == [
        console_input.PASTE_START + "BUG: body" + console_input.PASTE_END,
        console_input.PASTE_END,
    ]


def test_a_paste_that_landed_nothing_writes_no_stray_terminator(monkeypatch):
    # The other half of the rule above. With no records written at all the
    # session was never put into paste mode, so a bare ESC[201~ would be a
    # loose escape sequence arriving at an ordinary prompt — a mess of its own,
    # bought for a bracket that was never opened.
    session = live_session(monkeypatch, writes_at_most=0)

    assert console_input.paste(7, "BUG: body") is False
    assert session.writes == [
        console_input.PASTE_START + "BUG: body" + console_input.PASTE_END,
    ]
