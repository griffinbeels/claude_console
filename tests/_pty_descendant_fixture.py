"""A disposable PTY child whose group survives its leader. Never starts Claude."""
import json
import os
import signal
import sys
import time
from pathlib import Path

state = Path(sys.argv[1])
mode = sys.argv[2]
ready_read, ready_write = os.pipe()
signal.signal(signal.SIGHUP, signal.SIG_IGN)
descendant = os.fork()
if descendant == 0:
    os.close(ready_read)
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    os.write(ready_write, b"ready")
    os.close(ready_write)
    while True:
        time.sleep(1)

os.close(ready_write)
os.read(ready_read, 5)
os.close(ready_read)
temporary = state.with_suffix(".new")
temporary.write_text(json.dumps({"descendant": descendant, "group": os.getpgrp()}))
temporary.replace(state)
if mode == "leader-exits-first":
    os._exit(0)
while True:
    time.sleep(1)
