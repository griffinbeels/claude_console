"""Open a Claude session from the command line, for consumers that are not Python.

    python -m claude_console --cwd C:\\repos\\thing --prompt-file prompt.txt
    node  → spawn('python', ['-m','claude_console','--cwd',root,'--prompt-file','-'])

Exists because "importable from a project that shells out to Python from Node"
is a real requirement and a consumer should not have to write a Python file to
meet it. It is a thin wrapper over `open_session` and nothing else — no
behaviour lives here that the library does not have.

Two things about it are deliberate.

**The prompt comes from a file or stdin, never from argv.** argv has a length
ceiling, and a shell mangles the newlines that separate one instruction from
the next — which is most of what a prompt is.

**The pid is printed and flushed before anything is typed.** Typing means
attaching to the session's console, and attaching means *freeing this
process's own* (see console_input). A pid written after that point would be
written to a console this process no longer has. Piped or redirected stdout
survives either way, but a human running this in a terminal would lose it, and
the ordering costs nothing.
"""

import argparse
import sys
from pathlib import Path

from . import DEFAULT_LAUNCH, open_session


def _read_prompt(source: str | None) -> str:
    if source is None:
        return ""
    if source == "-":
        return sys.stdin.read()
    return Path(source).read_text(encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m claude_console",
        description="Open a visible Claude Code session and type a prompt into it.")
    parser.add_argument("--cwd", required=True,
                        help="working directory to open the session in")
    parser.add_argument("--prompt-file", metavar="PATH",
                        help="file holding the prompt to type; '-' reads stdin")
    parser.add_argument("--command", action="append", default=[], metavar="LINE",
                        help="a slash command to submit first, e.g. '/color green'. "
                             "Repeatable. Submitted in order, before the prompt")
    parser.add_argument("--launch", nargs=argparse.REMAINDER, metavar="ARGV",
                        help="argv to run instead of "
                             f"{' '.join(DEFAULT_LAUNCH)}. Must come last")
    parser.add_argument("--open-only", action="store_true",
                        help="open the session and exit without typing anything")
    args = parser.parse_args(argv)

    prompt = _read_prompt(args.prompt_file)
    session = open_session(args.cwd, args.launch or None)

    # Before any attach. See the module docstring.
    print(session.pid, flush=True)

    if not args.open_only:
        # Blocking, not the daemon thread the library uses: this process is
        # about to exit, and a daemon thread dies with it.
        #
        # Delivery is best-effort, exactly as it is for a library caller — a
        # session that never shows a prompt box times out quietly. The exit
        # code reports that the session OPENED, which is the part this can
        # actually promise.
        session.deliver_now(prompt=prompt, commands=args.command)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
