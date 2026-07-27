# claude_console

Open a visible Claude Code session on Windows and type a prompt into it.

**Windows only.** Not "mostly portable" — the default-terminal handoff, console
input buffers and `CreateEnvironmentBlock` are the whole substance of this
module, and it raises on import anywhere else. Nothing in it pretends otherwise.

```python
import claude_console

session = claude_console.open_session(r"C:\repos\thing")
session.deliver(prompt="FEATURE: make the thing")
```

That is the entire common case. `open_session` returns as soon as the session
exists; `deliver` returns immediately and does its waiting on a daemon thread.

## Why this is a shared module

The code is short. The reasoning is not, and the reasoning is the asset.

Every non-obvious line here was bought with a debugging session, and the
comments carry the measurements rather than the conclusions. The four that
matter most, because each of them fails *silently*:

- **The window belongs to the machine's default terminal, and that is
  deliberate.** Windows hands every new console to whatever the machine-wide
  "default terminal application" setting names — here, Windows Terminal.
  Nothing is prepended to the launch command to opt out of that. `conhost.exe`
  used to be, and it cost the session every glyph conhost cannot draw:
  **no monospaced font on this machine covers `U+23BF`**, the `⎿` Claude Code
  puts on every tool result line. Measured 2026-07-26 across every installed
  font — exactly three cover it and all three are proportional, which conhost
  cannot use.
- **Focus is opt-in, and a human gesture earns it.** A session opens because
  someone pressed a button, so its window may come to the front. What must open
  nothing at all is a *test*, and that is enforced rather than remembered:
  `tests/test_conventions.py` fails the build if any file but `session.py` asks
  for a new console in any spelling. This is the whole of the focus rule now —
  the `conhost` pin and the `hold_focus` watchdog that used to enforce a
  stricter version are gone.
- **Typing goes into the console's input buffer, which needs no focus.** True
  under Windows Terminal as well: measured 2026-07-26 against a WT-hosted
  console, `WriteConsoleInput` reaches the client and the screen buffer reads
  back verbatim. That is what lets a prompt be delivered to a window you are
  not looking at.
- **The session cannot wear Claude's icon, and the failure is silent.** Under
  WT, `GetConsoleWindow()` answers a `PseudoConsoleWindow` owned by the client,
  not the visible window that owns the taskbar button — so `WM_SETICON`
  *succeeds* against a window nobody sees. Sending it to the real Terminal
  window does change that window's icon and the taskbar still draws Terminal's,
  because a packaged app's button follows its AUMID manifest. Don't rebuild
  this; it was measured and it does not work.

`console_input` carries four more of the same kind — bracketed paste, waiting
for the prompt box, waiting for the *screen* rather than the clock between
writes, and giving up quietly. Read its module docstring before changing it.

## Where it lives, and why

**One copy, at `C:\Users\griff\Desktop\code\claude-console`, installed editable
into each consumer.** Not vendored, not junctioned, not copied.

```powershell
uv pip install --python "<consumer venv>\Scripts\python.exe" -e C:/Users/griff/Desktop/code/claude-console
```

An editable install writes a `.pth` and a path finder into the consumer's
`site-packages` pointing at *this* source tree. There is no second copy and
therefore no version to manage: edit a function here and the next process in
any consumer picks it up, and a brand-new file added to the package is
importable with no reinstall. That was measured, not assumed — it is the
property the whole layout is chosen for, because the intent is that every
consumer always runs the newest version of this.

The cost of that, stated plainly: **a breaking change here breaks every
consumer immediately**, with no pin to hide behind.

That is not left to memory. `consumers.json` lists every project that imports
this package, and a PostToolUse hook runs each one's test suite whenever
anything under `claude_console/` is written — a consumer going red blocks the
edit with its failing output. Run it by hand any time with:

```powershell
python tools/check_consumers.py
```

**Adopting this module in a new project is two steps:** install it editable
(below), then add a row to `consumers.json` so the guard covers you. Skip the
second and your project still works — it just stops being something a change
here is checked against.

Alternatives that were considered and rejected: a committed `sys.path` shim in
each repo (works, needs no install, but bakes an absolute machine path into
every consumer and is not a real package); a machine-wide `PYTHONPATH` (zero
setup, but invisible state that nothing in any repo mentions); vendoring or a
junction (two copies, or a coupling that has already gone wrong on this machine
once with `node_modules`).

### For a consumer with a pyproject

Declare it as a dependency with a path source, and the consumer's ordinary
setup command installs it:

```toml
dependencies = ["claude-console"]

[tool.uv.sources]
claude-console = { path = "C:/Users/griff/Desktop/code/claude-console", editable = true }
```

Then `uv pip install --python ".venv\Scripts\python.exe" -e .` — which `uv`
resolves through `[tool.uv.sources]`, verified.

**Absolute path, deliberately.** A relative one cannot serve both a repo root
and a git worktree under it: task_tracker's worktrees sit four levels deeper
than its main checkout, and the same `pyproject.toml` is checked out into both.

### For a consumer that shells out to Python from Node

The package is **stdlib-only** — no pyperclip, no yaml, nothing — precisely so
that it works under a bare system Python with no venv. Install it once into
whichever interpreter your tooling finds:

```powershell
uv pip install --python C:\Python314\python.exe -e C:/Users/griff/Desktop/code/claude-console
```

Then you need no Python file of your own:

```js
const child = spawn('python', ['-m', 'claude_console',
                               '--cwd', repoRoot,
                               '--prompt-file', '-'],
                    { windowsHide: true });
child.stdin.end(promptText);
// first line of stdout is the session pid
```

`--prompt-file -` reads stdin. The prompt never goes through argv: argv has a
length ceiling and a shell mangles the newlines that separate one instruction
from the next.

**What a consumer has to provide.** Not much, and it is worth being explicit,
because the tempting mistakes are all in this list:

1. **A working directory.** The session opens there and Claude reads its
   `CLAUDE.md` from there. It is not derived from anything.
2. **The prompt text, finished.** This module types exactly what it is given
   and adds nothing — no framing, no "when you are done, …". Build the string
   yourself.
3. **Its own fallback, and something that says so.** Delivery is best-effort
   and never raises: a session that never shows a prompt box just does not get
   typed into. Every consumer is expected to put the same text somewhere the
   user can reach — task_tracker copies it to the clipboard first — so that a
   failure costs one Ctrl+V rather than the text itself.

   **Pass `on_finish` and tell them when it happens.** An empty prompt box
   looks exactly like a hand-off that worked, and the person who opened the
   window is looking at the window, not at your app. `on_finish` is handed a
   `Delivery` on the background thread; `had_prompt and not prompt_typed` is
   the case worth a message, and a failed command usually is not.
4. **`safe_line()` on anything it did not author.** If you build a slash
   command out of text from a file, a form or a filename, clean it first. An
   unescaped `ESC[201~` closes the bracketed paste early and hands the rest of
   the string to a session running with `--dangerously-skip-permissions` as a
   command. `cap()` is there for the 60-character tab-label limit.

   **`safe_argument()` is its sibling, for a value going onto a command line
   rather than into a prompt box** — and you only need it if you build your own
   `launch`, since `name=` is cleaned for you. A launch crosses two parsers:
   quoting satisfies PowerShell, and then PowerShell hands the executable a
   second command line that the C runtime parses again. A double quote in the
   value splits the argument there, and since `claude [options] [command]
   [prompt]` takes a positional prompt — submitted the instant the session
   opens — the tail arrives as a message nobody wrote.
5. **A process that outlives delivery.** `deliver` uses a daemon thread, so a
   script that spawns a session and exits immediately types nothing. Use
   `deliver_now` if you are about to exit — `python -m claude_console` does.

## Surface

| Call | Does |
|---|---|
| `open_session(cwd, launch=None, name="")` | Spawn a session and resolve its pid. Returns `Session` |
| `Session.deliver(prompt, commands, on_finish=None)` | Submit each command, then leave the prompt typed and unsent. Background |
| `Session.deliver_now(prompt, commands)` | The same, on this thread, for a caller about to exit. Returns the `Delivery` |
| `Session.window()` | The console's `HWND`, or 0. Under Windows Terminal this is the *pseudo*-console window, not the visible one |
| `Session.pid` / `.host` | The session's pid / the `Popen` behind it — the same process |
| `console_input.Delivery` | What a delivery managed: `commands_submitted`, `commands_total`, `had_prompt`, `prompt_typed`, `seconds`, `.complete` |

**Name a session through `name=`, never by typing `/rename` yourself.** It
travels on the launch as `claude -n <name>`, so it is applied by the process
that draws the window — before this module types anything, and immune to a
slow start. A caller that supplies its own `launch` cannot have a flag injected
into its command line, so that session falls back to a typed `/rename` which
`deliver` puts in front of the commands for you. Either way you pass `name=`
and never build the command line or the slash command yourself.

`launch` defaults to `claude` running inside `powershell.exe -NoExit`, so when
Claude exits you are left at a PowerShell prompt in the session's directory
rather than watching the window and its scrollback disappear. An override
replaces the whole argv, wrapper included.

**When a hand-off misbehaves, read the log.** Every delivery appends what it
did to `%LOCALAPPDATA%\claude_console\delivery.log` — each step, how long it
took, and on failure the session's own screen, which is the artifact that
names the cause. `CLAUDE_CONSOLE_LOG` moves it.

Lower-level, all public: `spawn_claude`, `session_pid`, `default_launch`,
`display_name`, `unfocused_startup` (for spawning something the user did *not*
ask for — a window like that still may not activate), `claude_environment`,
`login_environment`, `safe_line`, `safe_argument`, `cap`, and the
`console_input`, `environment` and `journal` submodules.

## Tests

```powershell
uv venv --python 3.12 .venv
uv pip install --python ".venv\Scripts\python.exe" -e . pytest
& ".venv\Scripts\python.exe" -m pytest tests/ -q
```

**No test may put anything on screen.** The suite runs while someone is at the
keyboard. Exactly one test opens a real console — typing into another process's
console is OS behaviour and a mock of it would only assert that the mock was
called — and that console is windowless (`CREATE_NO_WINDOW`, which is still a
*real* console: `AttachConsole`, `WriteConsoleInput` and the screen buffer all
work against it). `tests/test_conventions.py` fails the build if anything else
asks for `CREATE_NEW_CONSOLE`, in any spelling.

The focus behaviour is pinned by
`test_nothing_but_the_session_itself_may_open_a_console_window` (the guard that
matters), plus `test_the_session_window_is_allowed_to_come_to_the_front` and
`test_a_helper_the_tool_spawns_for_itself_still_gets_no_focus` — the two halves
of "focus is opt-in", asserted against each other so neither can drift.
