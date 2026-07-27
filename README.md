# claude_console

Open a visible Claude Code session on Windows, type a prompt into it, and never
take the keyboard.

**Windows only.** Not "mostly portable" — `conhost.exe`, console input buffers
and `CreateEnvironmentBlock` are the whole substance of this module, and it
raises on import anywhere else. Nothing in it pretends otherwise.

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

- **Sessions are launched through `conhost.exe`, not directly.** Windows hands
  every new console to whatever the machine-wide "default terminal application"
  setting names. When that is Windows Terminal, WT creates the window itself,
  so the spawner's `STARTUPINFO` never reaches it and `SW_SHOWNOACTIVATE` is
  discarded. Measured 2026-07-26 from a console-less parent: a direct spawn
  moved the foreground to `CASCADIA_HOSTING_WINDOW_CLASS` within 400 ms and
  kept it; the same command through `conhost.exe` left the foreground untouched
  for the whole run. Naming the host opts out of the delegation, and leaves the
  user's terminal choice alone for everything else on the machine.
- **`SW_SHOWNOACTIVATE`, and a watchdog because asking is not enough.** Measured
  over ten spawns, two took the foreground anyway. `hold_focus` watches for
  ~1.5 s and hands the keyboard back — but only to the window that had it
  before, only when the thief is this session's console, and only once. It is
  short on purpose: past that the window belongs to the user, and deliberately
  clicking it is a human gesture that earns the focus it asks for.
- **Typing goes into the console's input buffer, which needs no focus.** That
  is what makes prompt delivery possible without stealing the keyboard, and it
  is why nothing in here ever calls `SetForegroundWindow` except to give focus
  *back*.
- **The pid you can type into is not the pid `Popen` gives you.** `spawn_claude`
  starts conhost; conhost starts Claude. `AttachConsole` refuses a console
  host's own pid, so `session_pid` walks the process tree for the child. Get it
  wrong and the rename, the colour, the prompt and the console font all fail at
  once and all quietly.
- **Pinning the host costs the window its icon, and `use_icon` buys it back.**
  A console takes its icon from the image its *host* was launched as, so
  `conhost.exe claude …` wears conhost's icon — measured byte-for-byte. Both
  `ICON_BIG` (taskbar, Alt+Tab) and `ICON_SMALL` (title bar) are set, through
  the attach the module already performs, with `SendMessageTimeoutW` rather
  than `SendMessageW` so a wedged console cannot park the delivery thread.

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
3. **Its own fallback.** Delivery is best-effort and fails silently by design:
   a session that never shows a prompt box just does not get typed into. Every
   consumer is expected to put the same text somewhere the user can reach —
   task_tracker copies it to the clipboard first — so that a timeout costs one
   Ctrl+V rather than the text itself.
4. **`safe_line()` on anything it did not author.** If you build a slash
   command out of text from a file, a form or a filename, clean it first. An
   unescaped `ESC[201~` closes the bracketed paste early and hands the rest of
   the string to a session running with `--dangerously-skip-permissions` as a
   command. `cap()` is there for the 60-character tab-label limit.
5. **A process that outlives delivery.** `deliver` uses a daemon thread, so a
   script that spawns a session and exits immediately types nothing. Use
   `deliver_now` if you are about to exit — `python -m claude_console` does.

## Surface

| Call | Does |
|---|---|
| `open_session(cwd, launch=None)` | Spawn, resolve the inner pid, start the focus watchdog. Returns `Session` |
| `Session.deliver(prompt, commands)` | Submit each command, then leave the prompt typed and unsent. Background |
| `Session.deliver_now(prompt, commands)` | The same, on this thread, for a caller about to exit |
| `Session.window()` | The console's `HWND`, or 0 |
| `Session.pid` / `.host` | The session inside the console / the host `Popen` |

The watchdog is started by `open_session` rather than left to the caller on
purpose: "nothing this opens may take the keyboard" is easy to state and easy
to forget, so there is no way to obtain a `Session` without it running.

Lower-level, all public: `spawn_claude`, `session_pid`, `unfocused_startup`
(useful for spawning anything unfocused, console or not), `foreground_window`,
`hold_focus`, `hold_focus_in_background`, `claude_environment`,
`login_environment`, `safe_line`, `cap`, `use_font`, and the `console_input`
and `environment` submodules.

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

The focus behaviour is pinned by: `test_the_session_is_launched_through_a_console_host_this_app_controls`,
`test_the_new_console_opens_without_taking_focus`, the three `hold_focus` cases,
`test_open_session_starts_the_focus_watchdog_itself`, and
`test_the_window_watched_is_the_one_that_had_focus_before_the_spawn`.
