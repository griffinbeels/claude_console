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
& ".venv\Scripts\python.exe" -m pytest tests/ -q     # 109 tests
```

- **PowerShell, not Bash — but the reason is narrower than it reads.** What
  Bash cannot resolve is the *relative backslash* form `.venv\Scripts\python.exe`,
  where `\S` and `\p` are escapes. An **absolute forward-slash** path works
  there: `"<this checkout>/.venv/Scripts/python.exe" -m pytest tests/ -q` ran
  green with `exit=0` (2026-07-27). Worth knowing
  because PowerShell mangles a native command's exit code, so a pass/fail that
  something else reads — a wrap step, a hook — is more honest out of Bash.
  PowerShell 5.1 also has no `&&`/`||` — chain with `;` or `if ($?) { }`.
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

  **A multi-file refactor is the other legitimate use, and it is the common
  one.** Removing a name takes several edits, and every state between the first
  and the last fails to import — so the guard fires on each one and prints a
  consumer traceback about a break that is thirty seconds old. Create the file
  before starting, delete it before the final edit, then run
  `python tools/check_consumers.py` by hand. Measured 2026-07-26: two blocked
  edits mid-way through deleting the font and icon machinery, neither of which
  said anything about the finished change.
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
| `task_tracker` (a sibling checkout; see `consumers.json`) | `open_session` (with `name=`), `Session.deliver` (with `on_finish=`), `console_input.Delivery`, `safe_line`, `cap`, `unfocused_startup`, `console_input.PASTE_END` | yes — `consumers.json` |
| `game-learnings` | not yet a consumer; add the row when it lands | — |

## Layout

| File | Owns |
|---|---|
| `__init__.py` | `Session`, `open_session`, and the public surface. The only place that composes spawn → resolve → watch into one call |
| `session.py` | The spawn — `DEFAULT_LAUNCH`, the rebuilt environment, and `unfocused_startup` for the helpers this module does *not* open on a user's behalf |
| `console_input.py` | Everything about typing into another process's console: bracketed paste, waiting for the prompt box, reading the screen back |
| `environment.py` | The environment Windows gives a freshly launched process |
| `text.py` | `safe_line`, `safe_argument` and `cap` — making a string safe to *submit as a line* and safe to *interpolate into a launch*, which are two different jobs with two different parsers downstream (invariants 11 and 15) |
| `journal.py` | The delivery log — where it lives, and that writing it can never break a hand-off |
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

| Rule file | Loads when you read | Holds |
|---|---|---|
| `.claude/rules/session.md` | `session.py`, `environment.py`, `__init__.py`, `__main__.py` | Invariants 1, 2, 3, 4, 5, 6, 12, 14, 15; what conhost cost |
| `.claude/rules/console-input.md` | `console_input.py`, `text.py`, `journal.py` | Invariants 7, 8, 9, 10, 11, 13 |
| `.claude/rules/tests.md` | `tests/*.py`, `tools/*.py`, `.claude/hooks/*.py` | What is covered, and the one test allowed to open a window |
| `.claude/rules/parallel-work.md` | always | Working on this alongside a consumer |

Most of those carry a `paths:` header, so they load **only when a matching file
is read** rather than in every session. Open the file you are about to change
and its rules arrive with it; read a rule directly whenever you want it sooner.
Measured on Claude Code 2.1.220 — the mechanism is `load_reason:
path_glob_match`, and a glob that matches nothing is a rule that silently never
loads, which `tests/test_documentation.py` fails the build on.

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
