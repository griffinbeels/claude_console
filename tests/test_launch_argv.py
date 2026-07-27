"""What `claude` really receives, measured instead of reasoned about.

The rest of the suite asserts the *string* `default_launch` builds, which is
where this bug hid: the string was correct. `-n 'Bug: the bar''s own "Spin up"'`
is exactly right for PowerShell's parser, and PowerShell then handed `claude` a
second command line that the C runtime split into two arguments. No assertion
about the text of a command line can see that, because the defect is in what
someone else does with it afterwards.

So these spawn a real PowerShell, running the real argv, with an argv-dumping
script standing in for `claude`, and read back the arguments it actually got.
Two deliberate substitutions, and nothing else:

* `claude` becomes the dumper. Everything about the quoting — the last element
  of the argv, which is the whole subject here — is the artifact's own.
* `-NoExit` comes out. It exists so a finished session leaves its window at a
  shell prompt, and a test that waits for that never returns.

Windowless (`CREATE_NO_WINDOW`), like everything else that spawns here: the
suite runs while someone is at the keyboard. See tests/test_conventions.py.
"""

import json
import subprocess
import sys

import pytest

from claude_console import session

# A real process, no window on screen. Not CREATE_NEW_CONSOLE — see
# tests/test_conventions.py, which fails the build on that flag anywhere but
# session.py.
NO_WINDOW = subprocess.CREATE_NO_WINDOW

# PowerShell loads the user's profile here exactly as a real launch does. It
# cannot change how PowerShell 5.1 quotes native arguments, but "the launch we
# ship, minus what would hang" is a shorter thing to keep honest than a list of
# flags this test decided were irrelevant.
SPAWN_TIMEOUT = 120

DUMPER = """import json, sys
print("ARGV=" + json.dumps(sys.argv[1:]))
"""


@pytest.fixture(scope="module")
def dumper(tmp_path_factory):
    path = tmp_path_factory.mktemp("launch") / "argv_dump.py"
    path.write_text(DUMPER, encoding="utf-8", newline="\n")
    return path


def _run(argv):
    finished = subprocess.run(argv, capture_output=True, text=True,
                              timeout=SPAWN_TIMEOUT, creationflags=NO_WINDOW)
    for row in finished.stdout.splitlines():
        if row.startswith("ARGV="):
            return json.loads(row[len("ARGV="):])
    raise AssertionError(
        f"the stand-in never reported its argv.\n"
        f"stdout: {finished.stdout!r}\nstderr: {finished.stderr!r}")


def launched_argv(name, dumper):
    """The arguments `claude` is really handed by `default_launch(name)`."""
    argv = [item for item in session.default_launch(name) if item != "-NoExit"]
    argv[-1] = argv[-1].replace(
        "claude --dangerously-skip-permissions",
        f'& "{sys.executable}" "{dumper}" --dangerously-skip-permissions', 1)
    return _run(argv)


def test_a_quoted_phrase_in_a_name_stays_one_argument(dumper):
    """The reported bug, and the reason `safe_argument` exists.

    Before the fix this came back as three arguments — `-n`, a name cut off at
    `Spin`, and `up restores ticks`, which `claude` reads as its positional
    prompt and submits the moment the session opens.
    """
    argv = launched_argv('Bug: the bar\'s own "Spin up" restores ticks', dumper)

    assert argv == ["--dangerously-skip-permissions", "-n",
                    "Bug: the bar's own Spin up restores ticks"]


def test_a_name_of_nothing_but_quoted_phrases_stays_one_argument(dumper):
    # Every pair of quotes is another place the argument can break, and the
    # single-quote case was passing on a name whose quotes had no space
    # between them, which is the one arrangement that cannot split.
    argv = launched_argv('Feature: "two words" and "two more" here', dumper)

    assert argv == ["--dangerously-skip-permissions", "-n",
                    "Feature: two words and two more here"]


def test_a_trailing_backslash_cannot_escape_the_quote_powershell_adds(dumper):
    # Measured: this used to arrive as `...backslash"`, the stray quote being
    # PowerShell's own closing one, escaped by the name's last character.
    argv = launched_argv("Bug: a title ending in a backslash\\", dumper)

    assert argv == ["--dangerously-skip-permissions", "-n",
                    "Bug: a title ending in a backslash"]


def test_an_apostrophe_still_survives_the_whole_round_trip(dumper):
    # The hop `powershell_quote` does cover, asserted here too so that a change
    # to either one cannot quietly break the other.
    argv = launched_argv("Griff's tasks", dumper)

    assert argv == ["--dangerously-skip-permissions", "-n", "Griff's tasks"]


def test_powershell_really_does_split_an_unescaped_quote(dumper):
    """The teeth. Nothing above can fail if this stops being true.

    Every other test here passes if `safe_argument` is deleted *and* PowerShell
    stops mangling quotes — so on its own the set cannot tell a working fix
    from a hazard that went away. This asserts the hazard directly, by quoting
    a raw name the way `default_launch` did before 2026-07-27.

    If it ever fails, the fix may be removable rather than broken: PowerShell 7
    passes native arguments literally under
    `$PSNativeCommandArgumentPassing`, so a machine that had switched default
    shells would land here first.
    """
    raw = 'Bug: an "unescaped phrase" here'
    argv = [item for item in session.DEFAULT_LAUNCH if item != "-NoExit"]
    argv[-1] = (f'& "{sys.executable}" "{dumper}" '
                f"-n {session.powershell_quote(raw)}")

    # Measured, not predicted: the break falls at the first space *outside* a
    # quoted run, which is the one after `unescaped` — not the one the reader
    # expects, and the reason this value is written down rather than derived.
    assert _run(argv) == ["-n", "Bug: an unescaped", "phrase here"], (
        "PowerShell 5.1 no longer splits a native argument on an interior "
        "double quote. safe_argument's quote removal may now be unnecessary — "
        "re-measure before trusting either behaviour."
    )
