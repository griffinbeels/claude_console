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
    # And confirmed from the console's own screen rather than from a fake of
    # it: the text was seen in the box, so the retry never ran.
    assert "pastes=1" in probe.stdout, probe.stdout


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


# The SAME layout under `"tui": "fullscreen"`, captured 2026-08-01 by writing
# a probe string into a live 2.1.220 session and reading the screen back. It
# draws "❯" and a NON-BREAKING space where the classic box draws "> ", for the
# sent message as well as for the box. Until that day `prompt_box` matched
# only ">", so it returned "" for every fullscreen window — which is not a
# visible break but a silent one: `submit` never sees its own echo, retries to
# exhaustion, and reports the command as undeliverable.
FULLSCREEN_SCREEN = (
    "❯\xa0We probably should have a hook literally type rename and color\n"
    "─" * 60 + "\n"
    "❯\xa0/color blue\n"
    + "─" * 60 + "\n"
    "  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← 1 agent\n"
)

FULLSCREEN_EMPTY = FULLSCREEN_SCREEN.replace("❯\xa0/color blue", "❯\xa0")


def test_the_fullscreen_tui_marker_is_read_as_a_prompt_box():
    assert console_input.prompt_box(FULLSCREEN_SCREEN) == "/color blue"


def test_an_empty_fullscreen_box_reads_as_empty_not_as_the_message_above_it():
    # The nbsp must strip, and the sent message one row up must not be
    # mistaken for typed text — otherwise `submit` would never see its line
    # leave the box and every command after the first would be abandoned.
    assert console_input.prompt_box(FULLSCREEN_EMPTY) == ""


def test_the_classic_marker_still_wins_where_it_is_the_one_drawn():
    # Additive, not a replacement: the bordered layout every delivery in the
    # log so far was typed into keeps behaving exactly as it did.
    assert console_input.prompt_box(LIVE_SCREEN) == "/color green"


class FakeSession:
    """A console that answers writes the way a live session was measured to.

    A bracketed paste lands in the prompt box; Enter and Escape empty it. The
    screen comes back in the real layout, so `prompt_box` is exercised rather
    than stubbed out.

    `reads_input` and `ignores_enter` are the two ways a real session stops
    keeping up — the whole reason the writes have to be paced by what the box
    shows rather than by a sleep.
    """

    def __init__(self, reads_input=True, ignores_enter=False, writes_at_most=None,
                 eats_pastes=0):
        self.box = ""
        self.reads_input = reads_input
        self.ignores_enter = ignores_enter
        # A paste the console accepts and the session never shows. This is the
        # reported failure in one flag: the write succeeds, the buffer takes
        # it, and it does not reach the box — so nothing but reading the box
        # back can tell it apart from a hand-off that worked.
        self.eats_pastes = eats_pastes
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
        elif text.startswith(console_input.PASTE_START) and self.eats_pastes:
            self.eats_pastes -= 1
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
    # No Enter, ever — that is the whole assertion. The line is written
    # COMMAND_ATTEMPTS times with a Ctrl+U between, because a session that
    # never shows the text is indistinguishable from one that dropped it.
    assert "\r" not in session.writes
    assert session.writes == [
        console_input.PASTE_START + "/color red" + console_input.PASTE_END,
        console_input.CLEAR_LINE,
        console_input.PASTE_START + "/color red" + console_input.PASTE_END,
    ]


def test_a_command_the_session_ate_is_written_again(monkeypatch):
    """The reported failure: "/color sometimes doesn't take".

    `delivery.log` 2026-07-27 06:14:44Z — `command did not submit:
    '/color purple'` after 9.7 s, which is submit's ECHO_TIMEOUT plus the wait
    for a prompt box. The write succeeded and the console buffer took it; the
    session, still starting, never drew it. Measured against a real windowless
    session the same day: `is_ready` goes true at 1.6 s while the box still
    holds its startup placeholder, so writing the instant it fires races a
    session that is not reading yet.

    `paste` was hardened against exactly this on 2026-07-26 and `submit` was
    not, which is why the prompt stopped getting eaten and the colour did not.
    """
    session = live_session(monkeypatch, eats_pastes=1)

    assert console_input.submit(7, "/color red") is True
    assert session.writes == [
        console_input.PASTE_START + "/color red" + console_input.PASTE_END,
        console_input.CLEAR_LINE,
        console_input.PASTE_START + "/color red" + console_input.PASTE_END,
        "\r",
    ]


def test_a_command_retry_clears_first_so_two_writes_cannot_concatenate(monkeypatch):
    # Measured on a live session: two pastes with nothing between them
    # concatenate. Without the Ctrl+U a retry turns one lost command into one
    # mangled one, which is worse than the failure it is fixing.
    session = live_session(monkeypatch, eats_pastes=1)
    console_input.submit(7, "/color red")

    assert session.writes[1] == console_input.CLEAR_LINE
    assert session.box == ""


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
    #
    # One attempt, because this is about the bracket rather than the retry —
    # the retries have their own tests below, and letting them run here would
    # bury the two writes being asserted under six more.
    opening = console_input._records_for(console_input.PASTE_START)
    session = live_session(monkeypatch, writes_at_most=opening + 4)

    assert console_input.paste(7, "BUG: body", attempts=1) is False
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

    assert console_input.paste(7, "BUG: body", attempts=1) is False
    assert session.writes == [
        console_input.PASTE_START + "BUG: body" + console_input.PASTE_END,
    ]


# Both captured from live sessions on 2026-07-26, because every rule in
# `box_shows` is about what Claude Code actually draws and none of it is
# guessable. Filler rows are dropped; nothing else is edited.
PLACEHOLDER_SCREEN = """
 ▐▛███▜▌   Claude Code v2.1.220
──────────────────────────────────────────────────────────── PROBE handover ──
> Try "create a util logging.py that..."
──────────────────────────────────────────────────────────────────────────────
  ⏵⏵ bypass permissions on (shift+tab to cycle) · ← for agents
"""

PASTED_BLOCK_SCREEN = """
 ▐▛███▜▌   Claude Code v2.1.220
─────────────────────────────────────────────────────────── PROBE multiline ──
> [Pasted text #1 +29 lines]
──────────────────────────────────────────────────────────────────────────────
  paste again to expand
"""

LONG_PROMPT = "\n".join(f"BUG: line {index}" for index in range(1, 31))


def test_an_empty_box_is_not_empty_on_a_fresh_session():
    # It holds a placeholder hint. So "the box has something in it" proves
    # nothing about whether OUR text arrived, which is why the confirmation
    # matches the text rather than the emptiness.
    assert console_input.prompt_box(PLACEHOLDER_SCREEN) != ""
    assert console_input.box_shows(PLACEHOLDER_SCREEN, "BUG: body") is False


def test_a_long_paste_is_recognised_by_the_placeholder_it_collapses_into():
    # A 30-line prompt is not drawn as its text at all. Matching the prose
    # would report a perfectly good hand-off as lost and then paste it again
    # on top of itself.
    assert console_input.box_shows(PASTED_BLOCK_SCREEN, LONG_PROMPT) is True


def test_a_screen_with_no_readable_box_is_never_taken_as_delivered():
    # Same fail-safe as is_ready and prompt_box: a layout this cannot read
    # costs a retry and then the clipboard, never a wrong claim of success.
    assert console_input.box_shows("nothing recognisable here", "BUG: body") is False


def test_a_prompt_the_session_ate_is_written_again(monkeypatch):
    """The reported bug, and the fix for it in one test.

    The write succeeds and the text never reaches the box. Nothing but reading
    the box back can see that, which is why this was silent for as long as it
    was: `paste` used to return the success of the *write*.
    """
    session = live_session(monkeypatch, eats_pastes=1)
    payload = console_input.PASTE_START + "BUG: body" + console_input.PASTE_END

    assert console_input.paste(7, "BUG: body") is True
    assert session.writes == [payload, console_input.CLEAR_LINE, payload]
    # Exactly one copy: measured, two pastes with nothing between them
    # concatenate into "…bodyBUG: body".
    assert session.box == "BUG: body"


def test_the_last_attempt_is_left_in_the_box_rather_than_cleared(monkeypatch):
    """A wrong confirmation must not empty a box that actually worked.

    If the text is landing and the *reading* is what is broken — a layout
    `prompt_box` cannot parse — then clearing on the way out turns a hand-off
    that arrived into an empty box. Clearing between attempts stops two copies
    concatenating; clearing after the last one has nothing left to protect.
    """
    session = live_session(monkeypatch, eats_pastes=99)
    payload = console_input.PASTE_START + "BUG: body" + console_input.PASTE_END

    assert console_input.paste(7, "BUG: body") is False
    assert session.writes[-1] == payload
    assert session.writes.count(console_input.CLEAR_LINE) == (
        console_input.PASTE_ATTEMPTS - 1)


def test_a_delivery_that_arrived_says_so(monkeypatch):
    live_session(monkeypatch)

    result = console_input.deliver(7, ["/color red"], "BUG: body")

    assert (result.commands_submitted, result.commands_total) == (1, 1)
    assert result.prompt_typed is True
    assert result.complete is True


def test_a_prompt_that_never_landed_is_reported_rather_than_swallowed(monkeypatch):
    # Quiet to the session is still the rule — nothing raises. Quiet to the
    # CALLER is what left a hand-off that typed nothing looking exactly like
    # one that worked.
    live_session(monkeypatch, eats_pastes=99)

    result = console_input.deliver(7, [], "BUG: body")

    assert result.prompt_typed is False
    assert result.complete is False


def test_a_command_that_was_abandoned_is_counted(monkeypatch):
    live_session(monkeypatch, ignores_enter=True)

    result = console_input.deliver(7, ["/rename A", "/color red"], "BUG: body")

    assert (result.commands_submitted, result.commands_total) == (0, 2)
    assert result.prompt_typed is True
    assert result.complete is False


def test_the_caller_is_told_when_it_is_over(monkeypatch):
    # The only way a consumer can say "it is on your clipboard": by then the
    # call that started this returned long ago.
    live_session(monkeypatch, eats_pastes=99)
    reported = []

    console_input.deliver_when_ready(7, [], "BUG: body", reported.append).join(10)

    assert [result.prompt_typed for result in reported] == [False]


def test_a_callback_that_raises_cannot_break_the_delivery(monkeypatch):
    live_session(monkeypatch)

    def explode(result):
        raise RuntimeError("the consumer's reporting is broken")

    thread = console_input.deliver_when_ready(7, [], "BUG: body", explode)
    thread.join(10)

    assert thread.is_alive() is False


def test_a_failed_delivery_records_the_screen_that_explains_it(
        monkeypatch, the_suite_never_writes_to_the_real_delivery_log):
    """The log is the deliverable, not a nicety.

    "Sometimes the prompt gets eaten" could not be answered from anything on
    this machine, because nothing recorded which of the ways it can be lost
    had happened. A failure now leaves the session's own screen behind, which
    is the one artifact that names a startup dialog, a box that already had
    something in it, or a layout nothing here could read.
    """
    live_session(monkeypatch, eats_pastes=99)

    console_input.deliver(7, [], "BUG: body")

    written = the_suite_never_writes_to_the_real_delivery_log.read_text(
        encoding="utf-8")
    assert "INCOMPLETE" in written
    assert "prompt attempt 3/3 did not reach the box" in written
    # The screen itself, indented under its label.
    assert "| > " in written


def test_logging_that_fails_never_takes_the_hand_off_with_it(monkeypatch):
    # A log that can break a delivery is worse than no log. `deliver` rather
    # than `paste`, because a paste that lands first time has nothing to say
    # and would not exercise the writer at all.
    monkeypatch.setenv("CLAUDE_CONSOLE_LOG", "Z:/no/such/drive/delivery.log")
    live_session(monkeypatch)

    assert console_input.deliver(7, [], "BUG: body").prompt_typed is True
