# claude_console — working notes for Claude

One copy of the code that opens a visible Claude Code session on Windows and
types into it. Extracted from task_tracker on 2026-07-26 because every project
on this machine wants it and none of them should rediscover the Windows
findings inside it.

`README.md` is written for a **consumer** — what to install, what to provide.
This file is for someone **changing** the module. Read both before editing.

## Run and test

```powershell
uv venv --python 3.12 .venv
uv pip install --python ".venv\Scripts\python.exe" -e . pytest
& ".venv\Scripts\python.exe" -m pytest tests/ -q     # 79 tests
```

- **PowerShell, not Bash.** The Bash tool on this machine cannot resolve
  `.venv\Scripts\python.exe`. PowerShell 5.1 has no `&&`/`||` — chain with `;`
  or `if ($?) { }`.
- **Windows only, and stated rather than implied.** `ctypes.WinDLL`,
  console input buffers and `CreateEnvironmentBlock` are the substance here; the
  package raises on import anywhere else. Nothing pretends to be portable.
- **Stdlib-only, and that is load-bearing.** It is what lets one editable
  install serve both a project with its own venv and a project that reaches
  whatever `python` is on PATH — here, a bare 3.14 with nothing installed in it.
  `test_the_package_has_no_third_party_dependencies` fails the build on a
  dependency being added. Verified working under 3.12 and 3.14.
- **`python -m claude_console --help`** is the CLI smoke test. Never run it
  without `--help` from a verification pass: it opens a real Claude window on
  the user's desktop.

## Changing this breaks other repos immediately — and a hook says so

There is no version to pin. Consumers install **editable**, so `site-packages`
holds a `.pth` and a path finder pointing at this tree — not a copy. An edit
here, including a brand-new file, is live in the next process everywhere. That
is deliberate and it is what was asked for. The cost runs the other way: this
repo's suite cannot catch a break, because it tests this module against itself.

**That check is automatic, not remembered.** `consumers.json` lists every
project that imports this package; `tools/check_consumers.py` runs each one's
suite; and `.claude/hooks/consumer_check.py` (PostToolUse, wired in
`.claude/settings.json`) fires it whenever anything under `claude_console/`
is written. A consumer that goes red **blocks with exit 2**, putting the failing
output in front of whoever made the edit, in the same turn.

```powershell
python tools/check_consumers.py     # the same check, by hand
```

- **Adding a project is one entry in `consumers.json`.** No code change; the
  hook picks it up on the next edit. That is the whole answer to "how do I stop
  another project quietly missing out".
- **Scope is only `claude_console/`.** Editing a test, the README or the hook
  itself changes nothing a consumer imports, and paying five seconds for that
  would train everyone to switch the guard off.
- **It fails open, deliberately** — a missing checkout, an absent venv, a wedged
  run. A consumer nobody has cloned is not a broken machine, and a guard that
  cries wolf is a guard that gets disabled within a day.
- **Escape hatch:** create `.claude/skip-consumer-check`. It is a file rather
  than a magic phrase because a PostToolUse edit has no command string to put a
  phrase in. Use it when the break is deliberate and the consumer is next on
  your list, and delete it when that lands.
- `tests/test_consumer_check_hook.py` is the block/allow corpus — re-run it
  after any edit to the hook, including `test_the_hook_is_actually_wired_up`,
  which is the only thing that notices `settings.json` losing the registration.

**Everything the guard prints must be ASCII.** Python writes stderr in the
system codepage (cp1252 here), so an em dash arrives as `?` — measured, in a
live run of this hook, in the very text meant to explain a failure. Docstrings
are exempt; they never reach a pipe. Pinned by
`test_everything_the_guard_prints_is_ascii`.

| Consumer | Uses | Verified by the hook |
|---|---|---|
| `task_tracker` (`C:\Users\griff\Desktop\code\task_tracker`) | `open_session`, `Session.deliver`, `safe_line`, `cap`, `unfocused_startup`, `console_input.PASTE_END` | yes — `consumers.json` |
| `game-learnings` | not yet a consumer; add the row when it lands | — |

## Layout

| File | Owns |
|---|---|
| `__init__.py` | `Session`, `open_session`, and the public surface. The only place that composes spawn → resolve → watch into one call |
| `session.py` | The spawn — `DEFAULT_LAUNCH`, the rebuilt environment, and `unfocused_startup` for the helpers this module does *not* open on a user's behalf |
| `console_input.py` | Everything about typing into another process's console: bracketed paste, waiting for the prompt box, reading the screen back |
| `environment.py` | The environment Windows gives a freshly launched process |
| `text.py` | `safe_line` and `cap` — making a string safe to submit as a line |
| `__main__.py` | The CLI, for consumers that are not Python |

`console_input.py` is ~490 lines, past the ~300 this machine's style prefers, and
stays whole **deliberately**. Its process-global attach lock and its ctypes
structs are one tightly-bound concern, and it is the riskiest thing here to move.
It lost ~170 lines when the font and the icon went (invariants 3 and 4), which
also took the obvious split — those were the only parts that needed a console to
merely *exist* rather than to be a session reading its buffer. What is left is
one concern, so leave it alone.

## Invariants

Break one of these and the failure is silent. Each cost a debugging session, and
the measurements are in the comments beside the code — do not compress them out.

1. **Focus is opt-in, and only a human gesture earns it.** A session opens
   because someone pressed a button; that window is allowed to come to the
   front, and it should. What must open nothing at all is a **test** — the
   suite runs constantly while someone is at the keyboard.

   This replaced a stricter rule on 2026-07-26, and the correction came from
   the person it was written for: *"It's totally fine if it steals focus for a
   sec when I ACTUALLY USE THE TOOL. I just spawned up tasks! […] The PROBLEM
   is when you, Claude, are working on a task and running a whole bunch of
   tests."* The old rule — nothing this module opens may ever activate — bought
   a guarantee nobody wanted, and paid for it with the conhost pin below.

   **The half that matters is enforced, not remembered.**
   `tests/test_conventions.py` fails the build if any file but `session.py`
   names a new-console flag, in any spelling including a string constant.
   `unfocused_startup` survives for the other half: a window the user did *not*
   ask for still may not activate, which is what task_tracker's `restart.py`
   uses it for. `test_a_helper_the_tool_spawns_for_itself_still_gets_no_focus`
   and `test_the_session_window_is_allowed_to_come_to_the_front` assert the two
   halves against each other.

2. **No console host is named, and the absence is the design.** Windows
   delegates every *new* console to whatever `HKCU\Console\%%Startup` names.
   When that is Windows Terminal the request is brokered (`svchost` →
   `OpenConsole.exe`) and **WT creates the window itself**, so a spawner's
   `STARTUPINFO` never reaches it. `conhost.exe` used to be prepended to opt
   out of that.

   **It was removed because conhost cannot draw the session.** conhost accepts
   only a monospaced face, and **no monospaced font on this machine covers
   `U+23BF`** — the `⎿` Claude Code puts on every tool result line. Measured
   2026-07-26 over every installed font via `GlyphTypeface.CharacterToGlyphMap`:
   exactly three cover it (Noto Sans JP, Noto Serif JP, Segoe UI Symbol) and
   all three are proportional. There is no font to switch to, so the fix was to
   stop pinning the host and let the window be the terminal its user actually
   develops in.

   Everything the delivery path needs survives the move, and that was measured
   before it was relied on: `AttachConsole` succeeds, `_screen_text()` reads a
   WT-hosted console's screen back verbatim, and `WriteConsoleInput` reaches
   the client. What does *not* survive is in invariants 3 and 5.

3. **A session's window cannot wear Claude's icon, and the attempt fails
   silently.** Under WT, `GetConsoleWindow()` answers a `PseudoConsoleWindow`
   owned by the client — **not** the visible `CASCADIA_HOSTING_WINDOW_CLASS`
   window that owns the taskbar button. So `WM_SETICON` through the ordinary
   attach *succeeds*, against a window nobody sees.

   Sending it to the real Terminal window works at the window level and still
   does not help: measured 2026-07-26, the icons read back as `claude.exe`'s
   exactly (`[526089, 328471]` → `[706287053, 665590077]`) and the taskbar
   button kept drawing Terminal's, because a packaged app's button follows its
   AUMID manifest. Confirmed by eye — no API reads a taskbar button back.

   This was a working feature under conhost and it is a **knowingly accepted
   loss**. Do not rebuild it. The need it served — telling Claude windows apart
   — is met by the session's title, and `SetConsoleTitleW` is measured to push
   that through to the WT window. The unexplored option is a dedicated WT
   profile carrying the icon on the *tab*, which needs `wt.exe -w new -p …` and
   therefore a different way of finding the session's pid.

4. **The console is not dressed at all any more.** `use_font` went with the
   host pin: WT ignores `SetCurrentConsoleFontEx` outright (measured,
   `_apply_face` returns False against a WT-hosted console) and needs no help,
   since it draws every glyph conhost could not. `deliver` therefore starts at
   the long wait for a prompt box rather than at two attaches.

5. **The pid to type into is the `Popen` itself.** This used to walk the
   process tree, and had to while conhost sat in the middle: `AttachConsole`
   refuses a console host's own pid (measured: false for conhost, true for its
   child). With no host, walking is **actively wrong** — measured 2026-07-26,
   the first child of a freshly spawned shell was an incidental helper it had
   started, so the walk returned a pid nothing could be typed into.

   The PowerShell wrapper does not bring the problem back: `powershell.exe`
   shares the console it was given rather than making one, so attaching to it
   reaches the same screen buffer `claude` is painting. `DEFAULT_LAUNCH` uses
   `-NoExit`, so a finished session leaves a usable prompt instead of taking
   its own scrollback away — and leaves a window behind until it is closed,
   which is the one thing about this change that is a cost rather than a gain.

6. **A spawned session's environment is rebuilt, never filtered.** `Popen`
   inherits the spawning process's environment, and an app that spawns Claude
   sessions is usually itself started *from* a Claude session — which sets a
   batch of variables for its children. Inheriting them made the spawned session
   differ from a hand-opened one in ways that were all silent: `NO_COLOR=1`
   rendered it monochrome, `GIT_EDITOR=true` and `GIT_TERMINAL_PROMPT=0` left
   its git unable to open an editor or ask for credentials, and
   `CLAUDE_CODE_CHILD_SESSION` turned transcript saving off.
   `environment.login_environment()` calls Win32 `CreateEnvironmentBlock`
   instead. **Do not add a var to a strip-list** — the list belongs to upstream
   and grows; rebuilding makes tomorrow's addition absent by construction.
   Nothing is added back on top either.

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

10. **Give up quietly, and expect the caller to have a fallback.** Every failure
    after the process exists is silent by design — the spawn itself is the only
    thing that raises. That is only safe because consumers are told to put the
    same text somewhere reachable first (README, section 5). Do not add loud
    failure here without revisiting that contract.

11. **Anything submitted as a line goes through `safe_line` first.** A string
    carrying `ESC[201~` closes the bracketed paste early, leaving whatever
    follows outside the paste for the trailing `\r` to submit as a command — in
    a session usually spawned with `--dangerously-skip-permissions`. Whitespace
    is collapsed before controls are stripped, because `str.split()` removes
    `\n`, `\r` and `\x1c`-`\x1f` but leaves ESC, NUL, BEL and backspace. It
    strips rather than rejects: odd text should still reach the session, it just
    must not be able to submit a line.

12. **Resolve through the module at call time, never through an imported name.**
    `from .session import session_pid` binds the function *object* into the
    importing namespace, so a test patching `session.session_pid` patches a name
    the caller never reads — and the real one runs underneath a suite that
    believes it stubbed it out. That shipped during the extraction itself,
    against the focus watchdog `open_session` used to start, and two tests
    caught it. `open_session` therefore calls `_session.foo()`, and the reason
    is written into the function so nobody "tidies" it back.

## What conhost cost, kept so nobody re-derives it

The pin is gone (invariant 2) and so is everything built on it. This section is
the receipt: each line was bought with a debugging session, and every one of
them applies again the moment anyone considers pinning a host.

- **The font.** conhost font-links *some* missing glyphs but not the quadrant
  blocks `U+2596`–`U+259F` that Claude Code's logo is drawn from, and Consolas
  has none of them; Cascadia Mono has all eight. `_apply_face` had to read the
  face back and revert, because an unknown face is not refused — conhost
  silently picks something of its own.
- **`HKCU\Console\UseDx` at 1 and 2 changed the rendering not at all**, and `⎿`
  still drew as a box under both Consolas and Cascadia Mono. That is the defect
  that finally cost conhost the job.
- **A console takes its icon from the image its *host* was launched as**, so
  pinning conhost cost every session the Anthropic logo, and `use_icon` bought
  it back with `WM_SETICON` for both `ICON_BIG` and `ICON_SMALL` —
  `SendMessageTimeoutW` rather than `SendMessageW`, so a wedged console could
  not park the delivery thread.
- **An icon handle goes invalid the moment the process that supplied it exits**
  — neither `LR_SHARED` nor a module-resource load changes that, and
  `SetConsoleIcon`, which existed for exactly this, is gone from this machine's
  kernel32. It cost less than it read: the taskbar rasterises an icon when it is
  set and keeps drawing it, measured with two labelled consoles side by side.
  A dead handle costs only a *re-query* — an Explorer restart or a DPI change.
- **The alternative that owned its handle properly** was a shortcut carrying
  `IconLocation`, handed to conhost as `STARTF_TITLEISLINKNAME`. It costs
  `subprocess.Popen`, since Python's `STARTUPINFO` has no `lpTitle`.
- **Verify an icon by size as well as by pixels.** `ExtractIconExW` returns the
  *system* large and small metrics, and those are per-process DPI: a DPI-aware
  tracker at 150% wears 48×48 and 24×24 while a DPI-unaware probe extracts
  32×32 and 16×16. Comparing across that gap says "neither claude's nor
  conhost's" about an icon that is exactly claude's, and reads as the feature
  not working (2026-07-26 — it cost an hour). **This one is not about conhost
  at all** and applies to any icon comparison on this machine.

## Adding a feature

- **Decide which repo it belongs in first.** If it would be true of a session
  opened on a git diff or a form submission, it belongs here. If it mentions
  a consumer's own concepts — a task, a group, a bucket — it belongs there.
- **New public name:** add it to `__all__` in `__init__.py` *and* to the surface
  table in `README.md`. The README is what the next consumer reads.
- **Never re-export a new-console flag.** `session.NEW_CONSOLE` is deliberately
  absent from `__all__`: the conventions test greps for the name in every
  spelling including a string constant, and listing it tripped that guard, which
  was the guard working. The one legitimate use is one import away.
- **Keep the measurements in the comments.** The code here is short enough to
  rewrite from scratch; the reasoning is not, and it is the whole reason this is
  a shared module rather than a snippet everyone copies.

## Tests

- **No test may put anything on screen.** The suite runs while someone is at the
  keyboard. Exactly one test opens a real console — typing into another
  process's console is OS behaviour, and a mock of it would only assert that the
  mock was called — and that console is windowless (`CREATE_NO_WINDOW`, which is
  still a *real* console: `AttachConsole`, `WriteConsoleInput` and the screen
  buffer all work against it). `tests/test_conventions.py` fails the build if
  anything else asks for `CREATE_NEW_CONSOLE` in any spelling. That guard lives
  here rather than in a consumer precisely because it scans this repo's files: a
  guard left behind when its code moves out still passes, and covers nothing.
- **`_attached` frees the calling process's console in order to attach
  elsewhere** — and under pytest that is pytest's own. `test_console_input.py`'s
  autouse fixture refuses every attach by default; a test that wants one opts in
  by patching over it. An unattached call is worse than a failed one: `CONOUT$`
  is then the *user's* terminal rather than the session's.
- Mock at the boundary: `_write_input` and `_screen_text` — never
  `subprocess.Popen` for the typing paths.
- **Deliberately untested:** that a real Claude session reads what is typed.
  That was verified by hand against a live session on 2026-07-25 and is what
  produced invariant 8; automating it means spawning a window on the user's
  desktop, which is exactly what invariant 1 forbids a *test* to do.

## Parallel work

This repo is small and usually edited alone, so committing straight to `main` is
fine. If it ever needs a worktree, the rules are task_tracker's: branch from
local `main` HEAD, worktree gets its own `.venv`, run the suite from the
worktree root with a relative path.

**The cross-repo case is the one to watch.** A change here plus a change in a
consumer are two commits in two repos with no shared history, so nothing makes
them atomic. Land the consumer-compatible change here first, then the consumer
— a shared module that briefly leads its consumers is harmless, one that briefly
lags them breaks every window on the machine at once.
