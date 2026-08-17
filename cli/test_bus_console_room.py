#!/usr/bin/env python3
"""bus_console room-existence corpus: no-such-room / dir-without-log must REFUSE,
room-with-log (even empty) must pass. Bare `PASS ` idiom; exit 1 on any FAIL."""
import os
import shutil
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from bus_console import require_live_room

FAILURES = []


def check(name, cond, detail=""):
    print(("PASS %s" % name) if cond else ("FAIL %s %s" % (name, detail)))
    if not cond:
        FAILURES.append(name)


def refuses(bus, room):
    try:
        require_live_room(Path(bus), room)
        return None
    except SystemExit as e:
        return str(e.code)


bus = tempfile.mkdtemp(prefix="bus-console-test-")
os.makedirs(os.path.join(bus, "dead-room"))                      # dir, no chat.jsonl
os.makedirs(os.path.join(bus, "live-room"))
open(os.path.join(bus, "live-room", "chat.jsonl"), "w").close()  # log present, EMPTY

# 1. no such room dir -> refuse, and the message names the state + the live rooms
msg = refuses(bus, "ghost-room")
check("no-such-room-refuses", msg is not None)
check("no-such-room-names-state", msg and "no such room dir" in msg, msg)
check("no-such-room-lists-live", msg and "live-room" in msg and "dead-room" not in msg, msg)

# 2. room dir WITHOUT chat.jsonl (the measured 2026-08-17 'relay' trap) -> refuse
msg = refuses(bus, "dead-room")
check("dir-without-log-refuses", msg is not None)
check("dir-without-log-names-state", msg and "no chat.jsonl" in msg, msg)

# 3. positive control: room WITH chat.jsonl passes — even an EMPTY log is a real
#    room whose "(0 msgs)" is a true statement, not absence-rendered-as-data
check("empty-but-real-room-passes", refuses(bus, "live-room") is None)

shutil.rmtree(bus)
print("\nFAILURES: %s" % (FAILURES if FAILURES else "none"))
sys.exit(1 if FAILURES else 0)
