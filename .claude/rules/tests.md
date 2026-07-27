---
paths:
  - "tests/*.py"
  - "tools/*.py"
  - ".claude/hooks/*.py"
---

# Tests — including the one that may open a window

## Tests

- **No test may put anything on screen.** The suite runs while someone is at the
  keyboard. Two test files spawn a real process, and both are windowless
  (`CREATE_NO_WINDOW`, which is still a *real* console: `AttachConsole`,
  `WriteConsoleInput` and the screen buffer all work against it).
  `tests/test_conventions.py` fails the build if anything asks for
  `CREATE_NEW_CONSOLE` in any spelling. That guard lives here rather than in a
  consumer precisely because it scans this repo's files: a guard left behind
  when its code moves out still passes, and covers nothing.
- **Both of those spawn for the same reason: the behaviour under test belongs
  to something else.** `test_console_input.py` types into another process's
  console, which is OS behaviour that a mock could only assert was called.
  `test_launch_argv.py` reads back the argv PowerShell really hands a native
  executable, which is the one thing no assertion about the *text* of a command
  line can see — that string was correct for the whole of invariant 15's life.
  It substitutes an argv dumper for `claude` and drops `-NoExit` (which would
  never return), and nothing else: the quoting under test is the artifact's own.
- **`_attached` frees the calling process's console in order to attach
  elsewhere** — and under pytest that is pytest's own. `test_console_input.py`'s
  autouse fixture refuses every attach by default; a test that wants one opts in
  by patching over it. An unattached call is worse than a failed one: `CONOUT$`
  is then the *user's* terminal rather than the session's.
- Mock at the boundary: `_write_input` and `_screen_text` — never
  `subprocess.Popen` for the typing paths.
- **`tests/conftest.py` points the delivery log at `tmp_path` for every test,
  and that is a guard rather than tidiness.** Its default home is the user's
  own `%LOCALAPPDATA%`, and the log's entire value is being readable after a
  hand-off went wrong — a suite appending its fixtures to it would bury the
  one occurrence somebody needs. Redirecting centrally means a new test file
  inherits it instead of having to remember.
- **The real-console probe's child echoes what it reads into a `> …` row**,
  because `paste` now confirms from the screen. A child that read without
  drawing would fail that confirmation forever, and the honest fix is for the
  fake session to behave like the real one rather than for the test to opt out
  of the check — the layout it draws is the one captured from a live session.
  It also holds its console open for `LINGER_SECONDS` after the paste: a
  console that vanishes with the last character cannot be read back at all.
  That sleep keeps an artifact alive for the observer, which is the opposite
  of the one invariant 8 forbids.
- **Deliberately untested:** that a real Claude session reads what is typed.
  That was verified by hand against a live session on 2026-07-25 and is what
  produced invariant 8. The reason it stays out of the suite is **cost, not
  visibility** — every run would start a real session and wait out its boot.

  **This entry used to give the wrong reason, and the correction is the useful
  part.** It said automating it "means spawning a window on the user's desktop,
  which is exactly what invariant 1 forbids a *test* to do" — and that is
  false. `CREATE_NO_WINDOW` starts a **real Claude session with no window at
  all**: `AttachConsole` reaches it, `_screen_text()` reads back what it is
  drawing, and `WriteConsoleInput` types into it. Six of them were run that way
  on 2026-07-26, including five at once, and nothing appeared on screen.

  Everything in invariant 13 came out of those runs, and none of it was
  reachable by reasoning — the placeholder in an "empty" box, the
  `[Pasted text]` collapse, what Ctrl+U does to it, what a startup dialog does
  to `is_ready`. **When a hand-off misbehaves and the log does not settle it,
  this is the move**: spawn one windowless, drive the real functions, log to a
  file (never stdout — attaching frees this process's own console), and kill it
  afterwards. A measurement against a *mock* of the session's screen can only
  agree with whatever you already believed.
