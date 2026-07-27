"""Making a string safe to submit as a line to a Claude session.

This lives beside `console_input.submit` because it is a *precondition* of it,
not a fact about any one consumer's data. Anything you pass to `submit` is
written as `ESC[200~ … ESC[201~` and then has Enter pressed on it, in a session
that is usually spawned with `--dangerously-skip-permissions`. A caller that
builds a `/rename` argument out of text it did not write — a file's frontmatter,
a form field, a filename — has to clean it first, and a caller that re-derives
the cleaning is a caller that gets it subtly wrong.
"""

import re

# A tab label longer than this is unreadable. That is now the only reason for
# it: the second one used to be that a short pasted `/rename` argument is
# inserted literally while a long one collapses into a `[Pasted text]`
# placeholder, and nothing pastes a name any more — it rides on the launch as
# `claude -n` (session.default_launch). The limit still binds the typed
# `/rename` fallback, where that reasoning does hold.
SESSION_NAME_LIMIT = 60

# C0 and C1 control characters, including DEL. `str.split()` does not remove
# these — see `safe_line` for why a submitted line must not carry one.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")

# Backslashes at the very end of a value, which is the only place one is
# dangerous once every double quote has been removed — see `safe_argument`.
# Spaces are in the class so that the tail is taken in one pass: `a \ \` has
# two trailing runs, and removing only the last leaves the value ending in a
# backslash again, which is the thing this exists to prevent.
_TRAILING_BACKSLASHES = re.compile(r"[\s\\]*\\[\s\\]*$")


def safe_line(text: str) -> str:
    """Collapse whitespace and drop control characters, in that order.

    Required for anything passed to `console_input.submit`. That text ends up
    inside a line written as `ESC[200~ … ESC[201~` with Enter pressed on it, so
    a string containing `ESC[201~` would close the bracketed paste early,
    leaving whatever follows it outside the paste for that Enter to submit as a
    command of its own — into a session running with permissions skipped. Any
    source the consumer did not author can express that: a double-quoted YAML
    scalar in a hand-edited file writes `\\e` directly.

    Whitespace goes first because collapsing is what stops a raw newline
    submitting the line early; controls go second because `str.split()` removes
    `\\n`, `\\r` and `\\x1c`-`\\x1f` but leaves ESC, NUL, BEL and backspace
    untouched. Stripping rather than rejecting: text with a strange character in
    it should still reach the session, it just must not be able to submit a line.
    """
    return _CONTROL.sub("", " ".join(text.split()))


def safe_argument(text: str) -> str:
    """`text` as a value that survives being interpolated into a launch line.

    `safe_line`'s sibling, and the difference between them is which parser is
    downstream. A line handed to `console_input.submit` is read by Claude Code
    and `safe_line` is enough for it. A value interpolated into
    `session.DEFAULT_LAUNCH` is read by PowerShell — which `powershell_quote`
    handles — and then, when PowerShell hands the native executable its
    arguments, **read a second time by the C runtime**. Only the first of those
    two hops was ever covered, and the second one is where a session's name was
    lost.

    Measured 2026-07-27, by spawning the real `default_launch` argv with an
    argv-dumping stand-in for `claude`. PowerShell 5.1 re-quotes a native
    command's arguments by wrapping the value in double quotes and escaping
    nothing inside it, so an interior quote closes that wrapper early and the
    next space splits the argument in two::

        'Bug: the bar''s own "Spin up" restores ticks'
        -> ['-n', "Bug: the bar's own Spin", 'up restores ticks']

    The tail does not merely vanish. `claude`'s usage is
    `claude [options] [command] [prompt]`, so it lands on the positional
    `prompt` — and a positional prompt is *submitted the instant the session
    opens*. The window came up named `Bug: the bar's own Spin` with
    `up restores ticks` already sent as a message nobody wrote, which is how
    this was reported: the name is cut off and the last word of the title
    turned into its own prompt.

    A trailing backslash is the same defect reached from the other side: it
    escapes the closing quote PowerShell appends, so `…backslash\\` arrives as
    `…backslash"`. Internal backslashes are harmless once the quotes are gone,
    because the C runtime only treats one specially when it precedes a quote.

    Both are **removed rather than escaped**, and that is the deliberate half.
    Escaping them correctly means reproducing the C runtime's rules *and*
    predicting whether PowerShell chose to wrap this particular value — it only
    wraps one containing whitespace — and that prediction is an undocumented
    internal which PowerShell 7 changes again under
    `$PSNativeCommandArgumentPassing`. What is being protected is a tab label,
    already capped to 60 characters and already stripped of its own whitespace;
    two punctuation marks are not worth a quoting engine whose failure mode is
    a stray message in somebody's session.

    Quotes go before `safe_line` so that removing one cannot leave the double
    space behind it, and the trailing backslashes go after, so a value ending
    in `\\ ` is not left ending in a backslash. That tail is taken whitespace
    and all: dropping only the last run of `a \\ \\` puts the value straight
    back into the state this rejects.
    """
    return _TRAILING_BACKSLASHES.sub("", safe_line(text.replace('"', "")))


def cap(text: str, limit: int = SESSION_NAME_LIMIT) -> str:
    """Truncate to `limit`, appending a single ellipsis so the result lands on it.

    A single `…` rather than three dots, so a capped string is exactly `limit`
    characters long instead of `limit + 2`.

    A limit below 1 has no room even for the ellipsis, so it yields nothing at
    all. Without that guard `limit == 0` reaches `text[:-1]` — dropping one
    character and appending an ellipsis, i.e. returning very nearly the whole
    string for a limit of zero. No caller reaches it today; the next one
    passing a computed limit would.
    """
    if limit < 1:
        return ""
    if len(text) <= limit:
        return text
    return text[:limit - 1] + "…"
