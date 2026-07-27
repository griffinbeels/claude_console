---
paths:
  - "claude_console/console_input.py"
  - "claude_console/text.py"
  - "claude_console/journal.py"
  - "tests/test_console_input.py"
  - "tests/test_text.py"
---

# Typing into a session — the protocol, and how it proves itself

Invariants 7, 8, 9, 10, 11 and 13.

7. **Typed text is bracketed, and waits for the prompt box.** The console input
   buffer accepts input long before Claude is ready to read it. Unbracketed, a
   newline mid-text reads as Enter and sends the first line alone; unwaited, the
   text is answered into whatever dialog is on screen — a folder Claude has not
   been trusted in opens on a question whose default is Enter. Both failures are
   silent, and both are why `paste()` polls for `READY_MARKERS` first.

8. **Nothing is written to a console until the prompt box shows the last thing
   that was.** Two writes a session reads in one pass are not two events to it.
   `WriteConsoleInput` only queues records; when they are drained is the
   session's business, not the writer's. Measured against a live session: with a
   whole hand-off written back to back, a `\r` sitting between two bracketed
   pastes was read as *part of the paste* — `/rename …` and `/color green`
   merged onto one line, both Enters vanished, and the prose landed on the end
   of it. So `submit` writes the line, waits for it to appear in `prompt_box`,
   writes `\r`, and waits for it to leave again. **Only a wait on the screen
   proves anything**: a `time.sleep` measures the writer, not the reader, which
   is why the 0.5 s `SETTLE_SECONDS` this replaced could not fix it — no
   constant can. The condition-based version is also *faster*: 0.42 s for two
   commands and a prompt, against 1.0 s of unconditional sleeping.

9. **Commands are submitted; the prompt is not.** `deliver` presses Enter for
   every command line first, and only then pastes the prompt, which is left
   editable. Backwards, a command's Enter would land on the still-unsubmitted
   prompt and send the user's prose as a chat message. Ordered this way, a
   command that fails costs only itself: the remaining commands are abandoned,
   the prompt is attempted regardless. A command that timed out has its text
   still in the box, so `deliver` calls `clear` — **Ctrl+U**, measured; Escape,
   the obvious guess, does nothing to a typed line at all.

10. **Give up quietly to the session, and report to the caller.** Every failure
    after the process exists is silent by design — the spawn itself is the only
    thing that raises. That is only safe because consumers are told to put the
    same text somewhere reachable first (README, section 3).

    **Silent to the *caller* was a bug, and it was fixed on 2026-07-26.**
    `deliver` returns a `Delivery` and `deliver_when_ready` takes an
    `on_finish`, because there was no way for a consumer to learn that its
    fallback had become the only copy — an empty prompt box looks exactly like
    a hand-off that worked, and the person who opened the window is looking at
    the window. Nothing raises and nothing blocks; the caller is simply told.

11. **Anything submitted as a line goes through `safe_line` first.** A string
    carrying `ESC[201~` closes the bracketed paste early, leaving whatever
    follows outside the paste for the trailing `\r` to submit as a command — in
    a session usually spawned with `--dangerously-skip-permissions`. Whitespace
    is collapsed before controls are stripped, because `str.split()` removes
    `\n`, `\r` and `\x1c`-`\x1f` but leaves ESC, NUL, BEL and backspace. It
    strips rather than rejects: odd text should still reach the session, it just
    must not be able to submit a line.

13. **A prompt is proven to have arrived, and written again if it did not.**
    Every command was confirmed twice — seen in the box, then seen to leave it
    — while the prompt, the thing a hand-off exists to deliver, was written
    and forgotten. `paste` returned the success of the *write*. So every way
    the text could be lost was silent, unrecoverable and unrecorded, which is
    how "sometimes the prompt gets eaten" survived as a feeling about slow
    windows rather than a bug report.

    `paste` now writes, waits for the box to show its own text, and on failure
    clears and writes again — `PASTE_ATTEMPTS` times. Four measurements
    against live sessions (2026-07-26) hold it up, and it is wrong without any
    of them:

    - **An empty box is not empty.** A fresh session draws a placeholder
      (`Try "create a util logging.py that..."`), so "the box has something in
      it" proves nothing. The confirmation matches our own text.
    - **A long paste is not drawn as its text.** It collapses to
      `[Pasted text #1 +29 lines]` — a 30-line prompt did, a 3-line one did
      not — so matching the prose alone would call a good hand-off lost and
      then paste it again on top of itself. `PASTED_BLOCK` is the other half
      of `box_shows`.
    - **Ctrl+U takes a paste back out**, the collapsed block as well as a
      typed line. That is what makes a retry idempotent.
    - **Two pastes with nothing between them concatenate** —
      `…hand-offBUG: a single line hand-off`. That is what makes clearing
      first mandatory rather than tidy.

    The **last** attempt is deliberately not cleared: if the confirmation is
    what is broken rather than the write, the text is sitting in the box
    unseen, and clearing on the way out would empty a box that worked.

    `READY_TIMEOUT` went from 45 s to 180 s in the same change, and that is
    part of the same bug. A session is "not ready" for every second a startup
    dialog is up — measured, the workspace-trust question keeps `is_ready`
    False until a person answers it — so 45 s was a deadline after which the
    prompt was dropped for good.

    **A command is written again too, as of 2026-07-27, and that it was not is
    the lesson.** This entry hardened the prompt and left `submit` writing once
    and giving up — the same eaten-write failure, in the half nobody had
    measured, which is what "the colour effect sometimes doesn't trigger" was.
    `delivery.log` 06:14:44Z: `command did not submit: '/color purple'` after
    9.7 s, which is `ECHO_TIMEOUT` plus the wait for a prompt box.
    `_write_until_shown` is now the retry both callers reach, so there is one
    implementation with one named difference: `COMMAND_ATTEMPTS` is 2 against
    `PASTE_ATTEMPTS`' 3, because the prompt is the payload and a command is
    decoration on it.

    **Why a retry rather than a longer or smarter wait**, measured against a
    real windowless session (2026-07-27):

        is_ready after 1.6s -> True
        prompt_box() -> 'Try "write a test for <filepath>"'
        ...two seconds later...
        prompt_box() -> ''

    `is_ready` goes true while the box still holds its startup placeholder and
    the layout is still moving, so writing the instant it fires races a session
    that is not reading yet. No constant fixes that — the session is ready when
    it is ready.

    That same dump killed a plausible theory worth recording, because it is the
    one a reader will re-invent: **the slash-command popup does not break the
    read.** `prompt_box` handles the real layout correctly — the row is
    `'>\xa0/color purple'`, the separator a non-breaking space that `strip()`
    removes — and against a settled session the command appears within 0.25 s
    and Enter clears it within another 0.25 s. So a command `submit` cannot see
    is most likely one the session never took, which also means there is
    usually nothing left in the box for the prompt to be pasted on top of.
