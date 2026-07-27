"""Cleaning a string that is about to be submitted as a line.

Every one of these came from a real path: task titles, type names and group
names read out of hand-editable YAML frontmatter, and a name typed into a text
box. A consumer's data is always someone else's text.
"""

from claude_console import console_input
from claude_console.text import SESSION_NAME_LIMIT, cap, safe_argument, safe_line


def test_a_newline_never_reaches_the_command_line():
    # Unbracketed, a newline mid-line submits early and leaves the rest as a
    # stray prompt.
    assert safe_line("Rename\nthe spawned\tsession") == "Rename the spawned session"


def test_an_escape_never_reaches_the_command_line():
    # str.split() removes \n and \t but not ESC, NUL, BEL or backspace, and a
    # double-quoted YAML scalar in a hand-edited file can express \e.
    cleaned = safe_line("Rename\x1bthe \x00spawned\x07 session\x08")

    assert "\x1b" not in cleaned
    assert cleaned == "Renamethe spawned session"


def test_text_cannot_close_the_bracketed_paste_it_is_typed_inside():
    """The reason this function is public rather than a consumer's private helper.

    A submitted line goes out as PASTE_START + line + PASTE_END and is then
    followed by its own \\r. Text carrying an END marker would close the paste
    early, putting everything after it outside the paste for that \\r to submit
    — into a session spawned with --dangerously-skip-permissions.
    """
    cleaned = safe_line(f"Fix it{console_input.PASTE_END}/exit")

    assert console_input.PASTE_END not in cleaned
    assert "\x1b" not in cleaned
    assert cleaned == "Fix it[201~/exit"


def test_nothing_but_control_characters_cleans_to_nothing():
    # So a caller can treat the result as falsy and fall back, rather than
    # submitting a line of invisible characters.
    assert safe_line("\x1b\x00") == ""


def test_surrounding_whitespace_goes_too():
    assert safe_line("   ") == ""
    assert safe_line("  spaced  out  ") == "spaced out"


def test_capping_to_no_room_at_all_yields_nothing():
    # text[:limit - 1] with limit 0 is text[:-1] — very nearly the whole
    # string, for a limit of zero. The next caller passing a computed limit is
    # who this guard is for.
    assert cap("Rename the spawned session", 0) == ""


def test_a_capped_string_is_exactly_the_limit_long():
    # A single ellipsis rather than three dots, so the result lands ON the
    # limit instead of two characters past it.
    capped = cap("R" * 200, 20)

    assert len(capped) == 20
    assert capped.endswith("…")


def test_text_that_already_fits_is_returned_untouched():
    assert cap("short", 20) == "short"
    assert cap("R" * 60) == "R" * 60


def test_the_default_limit_is_the_session_name_limit():
    assert len(cap("R" * 200)) == SESSION_NAME_LIMIT


def test_a_double_quote_is_removed_before_it_can_split_an_argument():
    """Measured: PowerShell wraps a native argument and escapes nothing inside.

    So an interior quote closes that wrapper and the next space starts a new
    argument — which `claude` reads as its positional prompt and submits. The
    end-to-end proof is tests/test_launch_argv.py; this pins the rule itself.
    """
    assert safe_argument('the bar\'s own "Spin up" restores ticks') == (
        "the bar's own Spin up restores ticks")


def test_removing_a_quote_leaves_no_double_space_behind_it():
    # Quotes come out before the whitespace collapse, not after, or every
    # removed quote would leave the gap it was standing in.
    assert safe_argument('a " b') == "a b"


def test_a_trailing_backslash_is_removed_and_an_internal_one_is_kept():
    # Only a trailing run is dangerous: it escapes the closing quote
    # PowerShell appends. With every quote already gone, an internal
    # backslash can never precede one.
    assert safe_argument("ends in a backslash\\") == "ends in a backslash"
    assert safe_argument(r"C:\repos\x") == r"C:\repos\x"
    assert safe_argument("trailing then space \\ ") == "trailing then space"
    # Two runs, taken in one pass — removing only the last would put the value
    # back to ending in a backslash, which is the state being rejected.
    assert safe_argument("two runs \\ \\") == "two runs"


def test_safe_argument_does_everything_safe_line_does():
    # It is safe_line plus two rules, never an alternative to it: a name still
    # reaches a `/rename` line through display_name.
    assert safe_argument("tasks\nrm -rf /") == "tasks rm -rf /"
    assert safe_argument("\x1b[201~ done") == "[201~ done"
    assert safe_argument("\x1b\x00\x7f") == ""
