"""Conventions a reviewer would otherwise have to catch by eye.

This file travelled here with the code it guards, which is the point of it.
The guard below lived in task_tracker's own conventions suite and scanned that
repo's `*.py` — the moment this module moved out, the code it was written for
left its reach. A guard that stays behind is a guard that silently stops
covering anything.
"""

import ast
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent

IGNORED_TREES = {".venv", ".git", "__pycache__"}
PYTHON_SOURCES = sorted(
    path for path in REPO.rglob("*.py")
    if not IGNORED_TREES.intersection(path.parts)
)

# `session.py` opens the console a Claude session runs in, and that console IS
# the feature; `test_session.py` is where that one exception is asserted.
# Everything else here runs while someone is using the machine. This file names
# the flags in order to find them, so it cannot scan itself.
MAY_OPEN_A_CONSOLE_WINDOW = {"session.py", "test_session.py", Path(__file__).name}

# Every spelling that reaches CreateProcess asking for a new console: the
# subprocess constant, and this package's alias for it — which is the nearer of
# the two, since anything wanting a console can already import claude_console.
CONSOLE_WINDOW_FLAGS = {"CREATE_NEW_CONSOLE", "NEW_CONSOLE"}


def _new_console_references(module: Path):
    """Line numbers where `module` names a new-console flag, in any spelling.

    Parsed rather than grepped so that prose about the flag — the comments in
    `_console_probe.py` explaining why it was removed — does not count as a use
    of it. String constants DO count: `getattr(subprocess, "CREATE_NEW_CONSOLE",
    0)` is how `session.py` spells it, and a copy of that line is the likeliest
    way this comes back.
    """
    tree = ast.parse(module.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        named = (isinstance(node, ast.Attribute) and node.attr in CONSOLE_WINDOW_FLAGS
                 or isinstance(node, ast.Name) and node.id in CONSOLE_WINDOW_FLAGS
                 or isinstance(node, ast.Constant) and node.value in CONSOLE_WINDOW_FLAGS)
        if named:
            yield node.lineno


def test_nothing_but_the_session_itself_may_open_a_console_window():
    """A test that puts a window on screen is a bug in the test.

    **This is now the only thing enforcing the focus rule, and the rule it
    enforces is the whole of it.** `session.py` used to pin `conhost.exe` and
    hand back stolen focus, on the theory that nothing this module opens may
    ever activate. That was over-broad. Focus is opt-in, and a human gesture
    earns it: pressing a button to open a session is one, so that window is
    allowed to come to the front. Running the suite is not.

    Windows 11 delegates every *new* console to whatever is set as the default
    terminal application. When that is Windows Terminal, WT creates the window
    itself, so the spawner's STARTUPINFO never reaches it and
    `SW_SHOWNOACTIVATE` is silently discarded — a full, activated Terminal
    window opens for as long as the child lives. For a session that is the
    point; for a test it is the bug.

    `tests/_console_probe.py` used CREATE_NEW_CONSOLE, so every run of the
    suite flashed one over whatever the user was typing into. That is most of
    what "random windows keep popping up while Claude works" turned out to be
    (2026-07-25). CREATE_NO_WINDOW still creates a *real* console —
    AttachConsole, WriteConsoleInput and the screen buffer all work against it
    — so nothing that merely needs to reach a console needs to show one.

    This lives away from the code it guards on purpose: the pin in
    `tests/test_console_input.py` sits next to the probe, and a merge that
    reverted both would take the guard with it.
    """
    offenders = [
        f"{module.relative_to(REPO).as_posix()}:{lineno}"
        for module in PYTHON_SOURCES
        if module.name not in MAY_OPEN_A_CONSOLE_WINDOW
        for lineno in _new_console_references(module)
    ]

    assert not offenders, (
        "CREATE_NEW_CONSOLE opens a Windows Terminal window that no flag can "
        "soften, over whatever the user is doing. Use CREATE_NO_WINDOW — it is "
        "still a real console — at: " + ", ".join(offenders)
    )


def test_the_package_has_no_third_party_dependencies():
    """Stdlib-only is what lets one copy serve every consumer on this machine.

    task_tracker imports this from a Python 3.12 venv; game-learnings reaches
    it through whatever bare `python` its Node tooling finds, which here is a
    3.14 with nothing installed in it. A single third-party import would mean
    every consumer has to provision an environment before it can open a
    window, and the README's "install it and import it" stops being true.

    Checked against the declared dependencies rather than by scanning imports:
    that is the list a future `uv add` would grow, and it is what a consumer
    reads before deciding this is cheap to adopt.
    """
    pyproject = (REPO / "pyproject.toml").read_text(encoding="utf-8")

    assert "dependencies = []" in pyproject, (
        "claude_console must stay stdlib-only. A dependency here has to be "
        "installed by every consumer, including ones that only have a system "
        "Python on PATH."
    )
