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

# A tab label longer than this is unreadable, and it doubles as what keeps
# Claude Code inserting a pasted `/rename` argument literally rather than
# collapsing it into a `[Pasted text]` placeholder — a short line pastes as
# text, a long one pastes as an attachment.
SESSION_NAME_LIMIT = 60

# C0 and C1 control characters, including DEL. `str.split()` does not remove
# these — see `safe_line` for why a submitted line must not carry one.
_CONTROL = re.compile(r"[\x00-\x1f\x7f-\x9f]")


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
