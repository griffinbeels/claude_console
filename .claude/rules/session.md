---
paths:
  - "claude_console/session.py"
  - "claude_console/environment.py"
  - "claude_console/__init__.py"
  - "claude_console/__main__.py"
  - "tests/test_session.py"
  - "tests/test_launch_argv.py"
  - "tests/test_environment.py"
---

# Opening a session — the window, the launch, the environment

Invariants 1, 2, 3, 4, 5, 6, 12, 14 and 15, and what conhost cost.

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

12. **Resolve through the module at call time, never through an imported name.**
    `from .session import session_pid` binds the function *object* into the
    importing namespace, so a test patching `session.session_pid` patches a name
    the caller never reads — and the real one runs underneath a suite that
    believes it stubbed it out. That shipped during the extraction itself,
    against the focus watchdog `open_session` used to start, and two tests
    caught it. `open_session` therefore calls `_session.foo()`, and the reason
    is written into the function so nobody "tidies" it back.

14. **A name rides on the launch, never on the keyboard.** `claude -n <name>`
    is applied by the process drawing the window, before this module types a
    character; a typed `/rename` was two screen round-trips standing between a
    window opening and the tasks arriving in it, on the slowest part of a
    session's life. `default_launch` cleans and quotes it, so no consumer can
    get either wrong, and `open_session` keeps the typed fallback for a caller
    that brought its own argv — there is no safe way to inject a flag into
    someone else's command line. A consumer passes `name=` and builds neither.

15. **A value on the launch crosses TWO parsers, and quoting only covers the
    first.** `powershell_quote` is correct and was never the bug. PowerShell
    then hands `claude` its arguments as a *second* command line, which the C
    runtime parses again — and PowerShell 5.1 builds that one by wrapping the
    value in double quotes and escaping nothing inside it. So an interior quote
    closes the wrapper early and the next space outside a quoted run starts a
    new argument. Measured 2026-07-27 by spawning the real `default_launch`
    argv with an argv-dumping stand-in for `claude`:

        'Bug: the bar''s own "Spin up" restores ticks'
        -> ['-n', "Bug: the bar's own Spin", 'up restores ticks']

    **The tail is not dropped — `claude [options] [command] [prompt]` takes a
    positional prompt, and a positional prompt is submitted the instant the
    session opens.** So a task title containing a quoted phrase opened a window
    named `Bug: the bar's own Spin` with `up restores ticks` already sent as a
    message nobody wrote. That is the whole reported symptom: the name is cut
    off and the last word of the title becomes its own prompt. A trailing
    backslash is the same defect from the other side — it escapes the closing
    quote PowerShell appends, and `…backslash\` arrives as `…backslash"`.

    `text.safe_argument` removes both rather than escaping them, and
    `display_name` runs it so the `/rename` fallback inherits the same string.
    Escaping correctly would mean reproducing the C runtime's rules *and*
    predicting whether PowerShell wrapped this particular value — it only wraps
    one containing whitespace — which is an undocumented internal that
    PowerShell 7 changes again under `$PSNativeCommandArgumentPassing`. The
    thing being protected is a 60-character tab label whose whitespace is
    already collapsed.

    **No assertion about the text of a command line can catch this**, which is
    why it survived a suite that checks that string closely: the string was
    right. `tests/test_launch_argv.py` spawns a real PowerShell and reads back
    the argv `claude` is really handed, and it carries its own falsifier —
    `test_powershell_really_does_split_an_unescaped_quote` asserts the hazard
    still exists, so the guard cannot quietly become a test of nothing.

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
