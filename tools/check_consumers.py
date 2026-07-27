"""Run every consumer's test suite against the working copy of claude_console.

    python tools/check_consumers.py

Consumers install this package **editable**, so they read this source tree
directly and always have the newest code with no update step. The cost of that
is the whole reason this script exists: a change here is live in every consumer
the moment it is saved, and nothing announces which one it broke. This repo's
own suite being green is half the check.

Exit codes: 0 = every reachable consumer passed, 1 = at least one failed. A
consumer whose checkout or venv is missing is SKIPPED, not failed — a machine
that has not cloned task_tracker is not a broken machine, and a check that
cries wolf there would be turned off within a day.
"""

import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REGISTRY = REPO / "consumers.json"

# Long enough for a real suite, short enough that a wedged one does not park a
# hook forever. task_tracker's 323 tests take ~5s.
TIMEOUT_SECONDS = 300


def consumers() -> list[dict]:
    return json.loads(REGISTRY.read_text(encoding="utf-8"))["consumers"]


def run_one(consumer: dict) -> tuple[str, str]:
    """(status, detail) for one consumer — "pass", "fail" or "skip"."""
    root = Path(consumer["path"])
    interpreter = root / consumer["python"]
    if not root.is_dir():
        return "skip", f"no checkout at {root}"
    if not interpreter.is_file():
        # ASCII only in printed output: Python writes stderr in the system
        # codepage on Windows (cp1252), and this text can end up in front of
        # the model via the hook. An em dash here arrives as `?`.
        return "skip", (f"no interpreter at {interpreter} - run "
                        f'`uv pip install --python "{consumer["python"]}" -e .` there')

    finished = subprocess.run(
        [str(interpreter), *consumer["verify"]],
        cwd=str(root), capture_output=True, text=True,
        timeout=TIMEOUT_SECONDS,
        # No console window: this runs while someone is at the keyboard, and a
        # child spawned from a console-less parent gets a brand-new console
        # brokered to whatever the default terminal is.
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))

    if finished.returncode == 0:
        return "pass", (finished.stdout.strip().splitlines() or ["ok"])[-1]
    # stdout carries pytest's summary; stderr carries an interpreter-level
    # failure such as a missing import. Both matter, and which one is empty
    # depends on how it broke.
    return "fail", (finished.stdout + finished.stderr).strip()


def main() -> int:
    failures = []
    for consumer in consumers():
        try:
            status, detail = run_one(consumer)
        except subprocess.TimeoutExpired:
            status, detail = "fail", f"timed out after {TIMEOUT_SECONDS}s"
        except OSError as problem:
            status, detail = "skip", str(problem)

        if status == "fail":
            failures.append((consumer["name"], detail))
            print(f"FAIL  {consumer['name']}")
        else:
            print(f"{status}  {consumer['name']}: {detail.splitlines()[-1] if detail else ''}"
                  .rstrip(": "))

    for name, detail in failures:
        print(f"\n--- {name} ---\n{detail}", file=sys.stderr)

    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
