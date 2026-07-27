#!/usr/bin/env python3
"""PostToolUse hook: editing the shared package runs every consumer's suite.

Consumers install claude_console **editable**, so they read this source tree
directly — there is no version to bump and no update step, which is exactly
what was asked for. The cost is that a change here is live everywhere the
instant it is saved, and *nothing announces which consumer it broke*. You find
out later, in a different window, as a mystery.

This repo's own suite cannot catch that: it tests this module against itself.
A sentence in CLAUDE.md saying "run both suites" is prose, and prose is read
only if someone happens to read it. A hook fires on every matching write.

Scope is deliberately narrow — only files under `claude_console/`. Editing a
test, the README or this hook changes nothing a consumer imports, so paying
five seconds for it would train everyone to switch the hook off.

Blocks with exit 2, which feeds the failing output back to the model so it can
fix it in the same turn rather than reporting green work that is not.

Fails OPEN on anything unexpected: an unknown payload, a missing registry, an
unreadable interpreter. A guard must never brick the tool it guards, and a
consumer whose checkout is absent is not a failure — `check_consumers.py`
skips it rather than crying wolf, because a guard that cries wolf gets
disabled within a day.

Escape hatch: create `.claude/skip-consumer-check` in this repo. That is a
file rather than a phrase because a PostToolUse edit has no command string to
put a phrase in. Delete it when the deliberate breakage is resolved.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent.parent
PACKAGE = REPO / "claude_console"
CHECKER = REPO / "tools" / "check_consumers.py"
ESCAPE_FILE = REPO / ".claude" / "skip-consumer-check"

EDIT_TOOLS = {"Write", "Edit", "MultiEdit"}

# The consumer suites, plus interpreter startup. task_tracker's 323 tests take
# ~5s; this is the "something is wedged" bound, and it must stay under the
# hook's own timeout in settings.json or the message is lost.
TIMEOUT_SECONDS = 240

# ASCII only in everything this prints. Python writes stderr in the system
# codepage on Windows (cp1252), so an em dash reaches the reader as a
# replacement character -- and the reader here is the model being told what it
# just broke. Measured: the `-` below was an em dash and came back as `?`.
# The docstrings above are never printed and keep their punctuation.
REPORT = (
    "claude_console was edited and a consumer's test suite now fails.\n\n"
    "Consumers install this package editable, so this change is already live "
    "in them - there is no version pinning them to the previous behaviour.\n\n"
    "{detail}\n\n"
    "Fix it here, or update the consumer, before treating this change as done. "
    "If the breakage is deliberate and the consumer is being updated next, "
    "create {escape} to silence this until you delete it."
)


def _touches_package(tool_input: dict) -> bool:
    path = tool_input.get("file_path")
    if not isinstance(path, str):
        return False
    try:
        resolved = Path(path).resolve()
    except (OSError, ValueError):
        return False
    return PACKAGE in resolved.parents


def main() -> int:
    try:
        payload = json.load(sys.stdin)
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    if payload.get("tool_name") not in EDIT_TOOLS:
        return 0
    tool_input = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        return 0
    if not _touches_package(tool_input):
        return 0
    if ESCAPE_FILE.exists() or not CHECKER.is_file():
        return 0

    try:
        finished = subprocess.run(
            [sys.executable, str(CHECKER)],
            capture_output=True, text=True, timeout=TIMEOUT_SECONDS,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.TimeoutExpired):
        # The check itself is broken or wedged. That is not evidence the edit
        # is wrong, and blocking on it would make this hook the problem.
        return 0

    if finished.returncode == 0:
        return 0

    detail = (finished.stderr or finished.stdout).strip()
    print(REPORT.format(detail=detail, escape=ESCAPE_FILE), file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main())
