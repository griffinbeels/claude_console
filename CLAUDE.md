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
& ".venv\Scripts\python.exe" -m pytest tests/ -q     # 69 tests
```

- **PowerShell, not Bash.** The Bash tool on this machine cannot resolve
  `.venv\Scripts\python.exe`. PowerShell 5.1 has no `&&`/`||` — chain with `;`
  or `if ($?) { }`.
- **Windows only, and stated rather than implied.** `ctypes.WinDLL`, `conhost`,
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
| `session.py` | The conhost spawn, `unfocused_startup`, the focus watchdog, and resolving the real `claude` pid inside the host |
| `console_input.py` | Everything about typing into another process's console: bracketed paste, waiting for the prompt box, the font, the icon |
| `environment.py` | The environment Windows gives a freshly launched process |
| `text.py` | `safe_line` and `cap` — making a string safe to submit as a line |
| `__main__.py` | The CLI, for consumers that are not Python |

`console_input.py` is 660 lines, well past the ~300 this machine's style prefers,
and stays whole **deliberately**. Its process-global attach lock and its ctypes
structs are one tightly-bound concern; it was already the riskiest thing to move,
and splitting it in the same change would have doubled that risk for no gain. If
it is ever split, the seam is icon/font (which only need a console to *exist*)
against paste/submit (which need a session reading the buffer).

## Invariants

Break one of these and the failure is silent. Each cost a debugging session, and
the measurements are in the comments beside the code — do not compress them out.

1. **Nothing this module opens may take focus.** A session is opened
   mid-thought and mid-sentence; a console that activates itself swallows the
   next keystrokes into a window nobody was looking at. `spawn_claude` passes
   `unfocused_startup()` (`STARTF_USESHOWWINDOW` + `SW_SHOWNOACTIVATE`). Nothing
   needs the focus it would take — `console_input` writes to the console's input
   buffer, which does not require an active window. Any future spawn gets the
   same treatment.

2. **`SW_SHOWNOACTIVATE` is not enough on Windows 11; the host has to be named.**
   Windows delegates every *new* console to whatever is set as the default
   terminal application. When that is Windows Terminal, the request is brokered
   (`svchost` → `OpenConsole.exe`) and **WT creates the window itself**, so the
   spawner's `STARTUPINFO` never reaches it: a full, activated Terminal window
   opens regardless of `wShowWindow`. Launching through `conhost.exe` opts out
   of the delegation, so the window is a classic console whatever the user's
   setting is. Measured 2026-07-26 from a console-less parent with the default
   terminal set to Windows Terminal: a direct spawn moved the foreground to
   `CASCADIA_HOSTING_WINDOW_CLASS` within 400 ms and kept it; the same command
   through `conhost.exe` left the foreground untouched for the whole run.

   This pins **only** the window this module opens. The user's terminal choice
   for everything else is theirs, and nothing here depends on it any more —
   which is what broke the day that setting changed underneath the tracker.

3. **Asking is not a guarantee, so the ask is checked.** Even through conhost,
   two spawns in ten took the foreground anyway (measured 2026-07-26 over ten),
   apparently depending on how promptly whatever was in front was answering
   messages — a flake, which is worse than a rule, because it survives testing.
   `hold_focus` records the foreground *before* the spawn and hands it back if
   this session's console turns out to be holding it. It hands back only to that
   window, only when the thief is this console, and only once inside 1.5 s:
   deliberately clicking the new session is a human gesture and keeps its focus.
   What gets reversed is focus nobody asked for.

4. **`open_session` starts the watchdog; a caller must never have to.** This is
   the reason that function exists rather than a `spawn`/`resolve`/`watch`
   sequence. It was the consumer's job until 2026-07-26, which meant a second
   consumer could simply not do it — and invariant 3's failure is two windows in
   ten, which nobody reads as a missing call. `test_open_session_starts_the_focus_watchdog_itself`
   pins it. Nothing pinned it before the extraction, because the single call
   site was stubbed out by the test fixture.

5. **The pid to type into is conhost's child, not the `Popen`.**
   `AttachConsole` refuses a console host's own pid (measured: false for
   conhost, true for its child). Hand the wrong one on and the rename, the
   colour, the prompt and the console font all fail at once and all silently,
   because every one of them gives up quietly by design. `session_pid` walks the
   process tree; the first child is right whatever `launch` was, since a wrapper
   like `pwsh -c claude` shares the console it was given.

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

12. **The console is put on a font and an icon that Windows will not supply.**
    Pinning the host means a classic console, and conhost font-links *some*
    missing glyphs but not the quadrant blocks `U+2596`–`U+259F` — exactly what
    Claude Code's logo is drawn from, and Consolas has none of them. Cascadia
    Mono ships with Windows 11 and has all eight. `_apply_face` reads the face
    back and reverts if it did not take, because an unknown face is not refused
    — conhost silently picks something of its own. Separately, a console takes
    its icon from the image its *host* was launched as, so pinning conhost cost
    every session the Anthropic logo; `use_icon` puts both `ICON_BIG` and
    `ICON_SMALL` back via `SendMessageTimeoutW` (not `SendMessageW`, so a wedged
    console cannot park the delivery thread).

    Measured and **did not work**, so nobody spends the afternoon again:
    `HKCU\Console\UseDx` at 1 and 2 changed the rendering not at all, and `⎿`
    (`U+23BF`, on every tool result) still draws as a box — under Consolas too,
    so the font change costs nothing there. No monospace font on the machine has
    both that and the quadrants.

13. **Resolve through the module at call time, never through an imported name.**
    `from .session import hold_focus_in_background` binds the function *object*
    into the importing namespace, so a test patching `session.hold_focus_in_background`
    patches a name the caller never reads — and the real watchdog runs
    underneath a suite that believes it stubbed it out. That shipped during the
    extraction itself and two tests caught it. `open_session` therefore calls
    `_session.foo()`, and the reason is written into the function so nobody
    "tidies" it back.

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
  by patching over it. `use_font` unattached is worse still: `CONOUT$` is then
  the *user's* terminal, and it is the one that would change font.
- Mock at the boundary: `_write_input`, `_screen_text`, `_apply_face`,
  `_apply_icon` — never `subprocess.Popen` for the typing paths.
- **Deliberately untested:** that a real Claude session reads what is typed.
  That was verified by hand against a live session on 2026-07-25 and is what
  produced invariant 8; automating it means spawning a window on the user's
  desktop, which invariant 1 exists to prevent.

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
