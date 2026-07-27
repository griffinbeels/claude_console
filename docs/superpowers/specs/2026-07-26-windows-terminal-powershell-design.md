# A session opens in Windows Terminal, running PowerShell — design

**Date:** 2026-07-26
**Status:** approved
**Repos:** `claude-console` (this one), then `task_tracker`

## The problem

A handed-off session opens in a classic conhost window. That window is not what
this machine's owner develops in — his PowerShell runs in Windows Terminal —
and conhost cannot draw `⎿` (U+23BF), which Claude Code puts on every tool
result line, so every session is visibly wrong in a way his own terminal never
is.

Stated as "launch using powershell instead of windows terminal", which inverts
once the terms are pinned down. PowerShell is a *shell*; the window belongs to a
*terminal host*. This machine's `HKCU\Console\%%Startup` names Windows Terminal
(`DelegationTerminal = {E12CFF52-A866-4C77-9A90-F570A7AA2C6B}`), so **his
PowerShell is already WT-hosted** — and `session.py` pins `conhost.exe`
specifically to escape WT. The window he likes and the window this opens are
opposite ends of the same setting.

## Why the pin can go

`CONSOLE_HOST`, `hold_focus`, `unfocused_startup`-on-spawn and `use_font` all
exist to serve one invariant: nothing this module opens may take focus. That
invariant was over-broad. The rule as its owner states it:

> "It's totally fine if it steals focus for a sec when I ACTUALLY USE THE TOOL.
> I just spawned up tasks! […] The PROBLEM is when you, Claude, are working on a
> task and running a whole bunch of tests which test the functionality of
> opening/closing instances."

Which is the rule already written in `~/.claude/rules/spawned-processes.md` —
*focus is opt-in, and only a human gesture earns it.* A tracker button is a human
gesture. A test is not.

**The test half is already enforced and does not change.** `tests/_console_probe.py`
opens its console with `CREATE_NO_WINDOW`, and
`test_conventions.py::test_nothing_but_the_session_itself_may_open_a_console_window`
fails the build if any file but `session.py` names a new-console flag in any
spelling, string constants included. That guard is the one that was actually
protecting him, it was bought with a real bug, and it stays exactly as it is.

So the focus machinery protected a case nobody wanted protected, at the cost of
the window being the wrong one.

## What was measured

All on 2026-07-26, against a real WT-hosted PowerShell spawned from a
console-less parent — the shape `spawn_claude` would use.

| Question | Answer |
|---|---|
| Does WT take focus on spawn? | Yes — foreground moved to `CASCADIA_HOSTING_WINDOW_CLASS` at 0.062 s and kept it. **Now the wanted behaviour.** |
| Can a foreign process read the session's screen? | **Yes.** `_screen_text()` read back `MARKER-SCREEN-READ-7742` verbatim; 3629 chars. |
| Can a foreign process type into it? | **Yes.** `_write(pid, …)` landed `GOT:HELLO-FROM-PARENT` then `GOT:QUIT` in the client's stdin. |
| Does `AttachConsole` work? | Yes. |
| Does `session_pid` still resolve? | **No — it returns the wrong pid.** With no conhost in between, `Popen.pid` *is* the session; `_children_of` then returned an incidental child (a CIM/`Add-Type` helper). |
| Does `_apply_face` work? | **No**, returns False. WT ignores `SetCurrentConsoleFontEx`; font is per-profile. Not needed — WT draws every glyph. |
| What does `GetConsoleWindow()` return? | A **`PseudoConsoleWindow`** owned by the client, *not* the visible `CASCADIA_HOSTING_WINDOW_CLASS` window. |
| Does `hold_focus` still work? | No. It compares the foreground against `console_window(pid)`; under WT those can never be equal. |
| Can the taskbar show the Anthropic icon? | **No.** `WM_SETICON` on the real WT window genuinely changed the window icon — `icon_before [526089, 328471]` → `icon_after [706287053, 665590077]`, exactly `claude.exe`'s extracted handles — and the taskbar still drew Terminal's. Packaged apps take the taskbar icon from the AUMID manifest. Confirmed by eye, because no API reads it back. |
| Could a font rescue conhost instead? | **No.** Every installed font enumerated via `GlyphTypeface.CharacterToGlyphMap`: exactly three cover both U+23BF and all ten quadrant blocks — Noto Sans JP, Noto Serif JP, Segoe UI Symbol — and **none is monospaced**, so conhost can use none of them. |
| Does a defterm session get a WT profile? | **No.** `WT_SESSION` and `WT_PROFILE_ID` both null — WT treats the handoff as a raw connection, not a profile launch. |
| Can the window be renamed from inside? | **Yes.** `SetConsoleTitleW` renamed the WT window to `CLAUDE-SESSION-PROBE-31337`, and it was findable by that title among top-level windows. |

**The icon is a deliberate, known loss.** It was working under conhost and it
cannot work under WT. Accepted with the trade stated.

## The shape

`spawn_claude` stops naming a console host and stops asking the window not to
activate:

```
Popen([powershell.exe, -NoExit, -Command, "claude --dangerously-skip-permissions"],
      cwd=…, creationflags=CREATE_NEW_CONSOLE, env=login_environment())
```

`CREATE_NEW_CONSOLE` from a console-less parent is what reaches the
default-terminal handoff, so WT draws the window — the machine's own setting
decides, which is the point. Nothing pins a host any more.

**`-NoExit` is deliberate:** when Claude exits you keep a PowerShell prompt in
the session's directory instead of the window vanishing with its scrollback.
That is the "use PowerShell for everything" half of the request. The cost is
that a finished session leaves a window until it is closed, which is a change in
what accumulates on screen — called out here because this machine's owner
judges a tool by what it leaves behind.

**The profile is not loaded with `-NoProfile`.** Fidelity to his own shell is
the point; a slow profile only delays Claude's start.

### `session_pid` collapses

The Popen is the session. `_children_of`, `PROCESSENTRY32W`, `CLIENT_TIMEOUT`
and `CLIENT_POLL` all go with it. The PowerShell wrapper does not change this:
`powershell.exe` shares the console it was given, so attaching to it reaches the
same screen buffer that `claude` is painting — which is exactly why the old
docstring already declared a `pwsh -c claude` override safe.

`session_pid` stays as a public one-liner rather than disappearing, so
`open_session`'s three-step shape and the consumer-facing surface are unchanged.

## What is deleted

| Name | Why |
|---|---|
| `CONSOLE_HOST` | No host is pinned. |
| `hold_focus`, `hold_focus_in_background`, `foreground_window`, `_activate`, `FOREGROUND_WATCH`, `FOREGROUND_POLL` | Cannot work under WT, and the behaviour they enforced is not wanted. |
| `use_font`, `_apply_face`, `SESSION_FACE`, `FONT_TIMEOUT`, `CONSOLE_FONT_INFOEX`, `_TRUETYPE_FAMILY`, `_NORMAL_WEIGHT` | Measured no-op under WT; WT needs no help. |
| `use_icon`, `session_icons`, `_apply_icon`, `SESSION_IMAGE`, `ICON_TIMEOUT`, `WM_SETICON`, `ICON_SMALL`/`ICON_BIG`, `_icons`, `_SMTO_ABORTIFHUNG`, `_ICON_SEND_MS` | Paints a `PseudoConsoleWindow` nobody sees. |
| `_children_of`, `PROCESSENTRY32W`, `CLIENT_TIMEOUT`, `CLIENT_POLL`, `_TH32CS_SNAPPROCESS` | The Popen is the session. |

`deliver` loses its first two lines and starts at the commands.

**`unfocused_startup` and `SW_SHOWNOACTIVATE` stay**, no longer used by
`spawn_claude`. `task_tracker/restart.py` imports `unfocused_startup` to relaunch
the tracker windowlessly, and that is still the right rule for a window the user
did not ask for. The split is the design: *the session may come forward; a
helper the tool spawns for itself may not.*

## Consumer safety

`task_tracker` touches exactly nine names — `open_session`, `console_input`,
`session`, `claude_environment`, `unfocused_startup`, `SW_SHOWNOACTIVATE`,
`safe_line`, `cap`, `SESSION_NAME_LIMIT`. **Every deleted name is absent from
that list**, so this is not a breaking change for the only consumer, and
`tools/check_consumers.py` (fired by the PostToolUse hook on any
`claude_console/` write) proves it rather than asserting it.

Landing order is the one CLAUDE.md already gives: this repo first, then
`task_tracker`. A shared module that briefly leads its consumers is harmless.

## Telling Claude windows apart, without the taskbar

The icon is gone; the need it served is not — "really helpful for classifying
these windows as claude windows rather than normal terminal windows".

**In scope now:** the session's window title. `SetConsoleTitleW` is measured to
rename the WT window, and `task_tracker` already names every session via
`/rename`. Nothing new is built for this — it is noted so the next reader knows
the lever exists and where it was proven.

**Explicitly out of scope, and why:** a dedicated WT profile carrying
`claude.exe`'s icon and a tab colour would put the logo back on the *tab*. It
cannot be reached from this spawn shape — a defterm handoff gets no profile at
all (measured). It needs `wt.exe -w new -p Claude …`, which exits immediately
and hands off to the running WT process, so the session would have to be found
by a marker planted in its command line, and the profile would have to be
installed into WT's `settings.json` idempotently by this module so there is no
setup step to remember. That is a second increment, deliberately sequenced after
this one so the window is seen before more is spent on it.

## Testing

No test may put a window on screen; the conventions guard already enforces it
and is untouched.

**Deleted** (they pin behaviour that no longer exists): the console-host test,
the no-focus-on-open test, the fall-back-to-the-host test, the three `hold_focus`
tests, the two watchdog tests in `test_session.py`, the two font tests and the
five icon tests in `test_console_input.py`.

**Changed:** `test_the_typist_is_given_the_process_inside_the_console` and
`test_open_session_reports_the_process_inside_the_console` now assert the
session is the `Popen` itself. `test_spawn_skips_permission_prompts_by_default`
follows `DEFAULT_LAUNCH`'s new shape.

**Added:**

- the default launch runs `claude` inside `powershell.exe`, with `-NoExit`;
- a `launch` override replaces the whole argv, PowerShell wrapper included;
- the spawn passes no `startupinfo`, so the window is allowed to come forward;
- `unfocused_startup` still returns `SW_SHOWNOACTIVATE`, for the consumer that
  spawns windowless helpers with it.

**Deliberately untested, unchanged from today:** that a real Claude session reads
what is typed. Verified by hand against a live session; automating it means a
window on the desktop.

**The by-hand check for this change:** open one session from the tracker's
button. It should be a Windows Terminal window running PowerShell, `⎿` should
draw as a corner rather than a box, the Claude logo should render, the prompt
should arrive typed but unsent, and the window should come to the front — that
last one now being correct rather than a bug.

## Touch set

`claude_console/session.py`, `claude_console/console_input.py`,
`claude_console/__init__.py`, `tests/test_session.py`,
`tests/test_console_input.py`, `tests/test_conventions.py` (docstring only),
`CLAUDE.md`, `README.md`. Then in `task_tracker`: `CLAUDE.md` only — its
~120 lines of conhost and Windows-Terminal reasoning invert, including the
invariants it says are "enforced by code in `claude_console`". No `task_tracker`
code changes.
