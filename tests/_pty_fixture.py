"""Tiny interactive CLI for no-window PTY tests. Never starts Claude."""
import json
import os
import sys
import time
import tty
from pathlib import Path

path = Path(sys.argv[1])
mode = sys.argv[2]
tty.setraw(0)
text = ""
submitted = []


def draw():
    screen = f"❯ {text}\n? for shortcuts"
    temporary = path.with_suffix(".new")
    temporary.write_text(json.dumps({"screen": screen, "prompt": text,
                                      "submitted": submitted,
                                      "cwd": os.getcwd()}), encoding="utf-8")
    temporary.replace(path)
    os.write(1, ("\x1b[2J\x1b[H" + screen.replace("\n", "\r\n")).encode())


if mode == "delayed":
    time.sleep(.15)
if mode == "dialog":
    path.write_text(json.dumps({"screen": "❯ Yes, I trust this folder\nEnter to confirm",
                                "prompt": "", "submitted": []}), encoding="utf-8")
    os.write(1, b"Trust this folder?\r\n")
else:
    os.write(1, b"\x1b[?2004h")
    draw()

pending = b""
while True:
    incoming = os.read(0, 65536)
    if not incoming:
        break
    pending += incoming
    while pending:
        if pending.startswith(b"\x1b[200~"):
            end = pending.find(b"\x1b[201~", 6)
            if end < 0:
                break
            value = pending[6:end].decode("utf-8")
            pending = pending[end + 6:]
            if mode != "drop":
                text += value
                draw()
        elif pending.startswith(b"\r"):
            pending = pending[1:]
            if mode != "stuck":
                submitted.append(text)
                text = ""
                draw()
        elif pending.startswith(b"\x15"):
            pending = pending[1:]
            text = ""
            draw()
        else:
            # Wait for a fragmented paste marker; reject unexpected input.
            if b"\x1b[200~".startswith(pending):
                break
            raise SystemExit(f"Unexpected input: {pending!r}")
