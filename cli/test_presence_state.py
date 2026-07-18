#!/usr/bin/env python3
"""Corpus lock for presence_state (Eve review F2 + friendly-fire from 6c719ead).

The attending/idle/offline label is a real instrument — peers make re-ping
decisions on it. NULL-CONTROL-AT-BIRTH says the control ships with the
instrument; this is that control. Separate file (not test_chat.py) so it does
not tangle the held signing lane's test changes → the operability commit stays
cleanly cherry-pickable. Plain script (run directly), matching test_chat.py's
style; exits non-zero on any failure.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from chat import presence_state  # noqa: E402

WINDOW = 300.0      # beat fresh within 300s
ATTENTION = 300.0   # message counts as recent within 300s

CASES = [
    # (beat_age, msg_age, expected, why)
    (10,   5,    "attending", "fresh beat + fresh message = genuinely here"),
    (10,   None, "idle",      "fresh beat, NEVER sent = process alive, nobody reading"),
    (10,   3600, "idle",      "fresh beat, hour-old message = away (the re-ping trap)"),
    (3600, 5,    "offline",   "stale beat wins even with a recent message"),
    (3600, None, "offline",   "stale beat, never sent"),
    # boundaries (<=, so exactly-on-the-edge is still fresh/recent)
    (300,  300,  "attending", "both exactly on the edge = still attending"),
    (301,  5,    "offline",   "beat one second past window = offline"),
    (10,   301,  "idle",      "message one second past attention = idle"),
    (0,    0,    "attending", "just beat + just spoke"),
]


def main() -> int:
    failed = 0
    for beat_age, msg_age, expected, why in CASES:
        got = presence_state(beat_age, msg_age, WINDOW, ATTENTION)
        ok = got == expected
        failed += not ok
        print(f"  {'ok  ' if ok else 'FAIL'} {expected:<9} "
              f"(beat={beat_age}, msg={msg_age}) — {why}"
              + ("" if ok else f"  [got {got!r}]"))
    # a distinct attention window must actually change the verdict (guards a
    # future refactor that ignores the param)
    if presence_state(10, 200, WINDOW, 100.0) != "idle":
        print("  FAIL attention param is not load-bearing (msg_age>attention should be idle)")
        failed += 1
    else:
        print("  ok   attending→idle flips when attention window tightens")

    print(f"\n{'OK: all presence_state cases passed' if not failed else f'{failed} FAILED'}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
