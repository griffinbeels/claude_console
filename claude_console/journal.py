"""An append-only record of what a delivery did, for the next "sometimes".

Every failure after the spawn is quiet by design — the caller has the same
text on the clipboard, so a timeout costs one Ctrl+V rather than the text
itself. Quiet to the *user* is right. Quiet to *disk* is what made "sometimes
the prompt gets eaten" unanswerable: nothing anywhere recorded which of the
ways it can be lost had happened, so the only evidence was a person noticing
an empty box minutes later and remembering that the window had felt slow.

So the delivery path writes what it saw here, and writing it can never be the
thing that breaks a hand-off: every call swallows its own errors. A log that
can take the session down is worse than no log.

The screen text goes in on failure, because that is the one artifact that
names the cause — a startup dialog, a box that already had something in it, a
layout `prompt_box` could not read. Reading it back afterwards is the whole
point of the file existing.
"""

import os
import time
from pathlib import Path

# Half a megabyte holds weeks of hand-offs, and a failure writes ~30 lines.
MAX_BYTES = 512 * 1024


def log_path() -> Path:
    """Where the delivery log lives, resolved at call time.

    A function rather than a constant so tests can point `CLAUDE_CONSOLE_LOG`
    somewhere harmless — a module-level constant would capture the real path
    at import and make the suite write to the user's own log.
    """
    override = os.environ.get("CLAUDE_CONSOLE_LOG")
    if override:
        return Path(override)
    local = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
    return Path(local) / "claude_console" / "delivery.log"


def record(message: str) -> None:
    """Append one entry, and never raise.

    Timestamps are UTC: a hand-off is compared against other logs on this
    machine, and local time makes that comparison wrong twice a year.
    """
    try:
        path = log_path()
        path.parent.mkdir(parents=True, exist_ok=True)
        _rotate(path)
        stamp = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())
        with path.open("a", encoding="utf-8", newline="\n") as handle:
            handle.write(f"{stamp}Z {message}\n")
    except Exception:
        # A hand-off that fails because its logging failed would be a worse
        # bug than the one this file exists to diagnose.
        pass


def _rotate(path: Path) -> None:
    """Keep one previous generation, so the file cannot grow without bound."""
    try:
        if path.exists() and path.stat().st_size > MAX_BYTES:
            os.replace(path, path.with_suffix(path.suffix + ".1"))
    except OSError:
        pass
