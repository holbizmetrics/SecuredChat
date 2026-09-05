"""test_cold_cursor.py — "0 pending" must never mean "0 since one second ago".

THE INCIDENT (2026-09-05, termux-claude-4c248004, reported over the bus). A fresh
session ran boot step 11, was told "0 pending", and concluded nothing was waiting.
2,911 messages sat unread, one of them a broadcast saying the exact open item it
was about to attack had already been done by another box the day before. It did
the work anyway: a full session of mathematics, duplicated.

Nothing was hidden. The count was printed, one line ABOVE the verdict:

    securedchat: fresh identity ... cursor anchored at HEAD; 2911 historical
    message(s) skipped (replay: --from-start or --since <id>)
    0 pending

The defect is that the verdict was computed from a quantity other than the one
being reported on. A reader who reads verdicts — which is what a verdict is for —
gets a false one. The same shape hit linux-claude-5534b575 the same night in
another room (11 skipped, "0 pending"), so n=2 before either was fixed.

TWO CHANGES, each with its own arm below:
  1. A message a peer EXPLICITLY ADDRESSED to this identity is no longer skipped
     by the cold anchor. Broadcasts still are — a newcomer is not owed a room's
     whole history — but they are counted.
  2. The verdict line carries the skipped counts, so "0 pending" cannot stand
     alone after a cold anchor.

CONTROL DISCIPLINE: the warm-cursor arm exists to prove the change did NOT just
make every verdict noisy. If a normal "0 pending" ever grows a cold-anchor
suffix, that arm goes red.
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile

HERE = pathlib.Path(__file__).resolve().parent
CHAT = HERE / "chat.py"
FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def git(repo, *a):
    return subprocess.run(["git", "-C", str(repo), *a], capture_output=True, text=True)


def make_bus(tmp):
    root = pathlib.Path(tmp) / "bus"
    root.mkdir(parents=True)
    (root / ".securedchat-bus").write_text("test\n")
    git(root, "init", "--initial-branch=main")
    git(root, "add", "-A")
    git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    return root


def run(bus, identity, cfg, *a):
    env = dict(os.environ)
    env["XDG_CONFIG_HOME"] = str(cfg)        # isolate the cursor store per arm
    env["HOME"] = str(cfg)
    return subprocess.run(
        [sys.executable, str(CHAT), "--bus", str(bus), "--room", "r",
         "--identity", identity, "--transport", "file", *a],
        capture_output=True, text=True, env=env)


def seed(bus, rows):
    """Write chat.jsonl directly: these are messages that existed BEFORE our
    identity did, which is precisely the situation the cold anchor is for."""
    room = bus / "r"
    room.mkdir(parents=True, exist_ok=True)
    with (room / "chat.jsonl").open("w", encoding="utf-8") as fh:
        for i, (frm, to, body) in enumerate(rows):
            fh.write(json.dumps({
                "ts": 1788500000.0 + i, "id": f"{i:08d}-0000-4000-8000-{i:012d}",
                "from": frm, "to": to, "kind": "msg", "body": body}) + "\n")


def main():
    print("test_cold_cursor")

    # ---- THE INCIDENT: history exists, none of it addressed to us ---------- #
    with tempfile.TemporaryDirectory() as tmp:
        bus = make_bus(tmp)
        cfg = pathlib.Path(tmp) / "cfg"; cfg.mkdir()
        seed(bus, [("peer-a", None, "broadcast: the RH open item is already done"),
                   ("peer-b", None, "another broadcast"),
                   ("peer-c", "someone-else", "not for us")])
        r = run(bus, "fresh-claude-1111", cfg, "recv", "--summary")
        out = r.stdout
        check("CONTROL: the cold anchor still fires (history was present)",
              "cursor anchored at HEAD" in out)
        check("THE CASE: the verdict does NOT stand alone as a bare '0 pending'",
              "0 pending (cold anchor:" in out)
        check("the verdict names how many were skipped",
              "3 historical" in out)
        check("and separates broadcast from addressed-to-you",
              "0 addressed to you" in out and "3 broadcast" in out)

    # ---- A message someone SENT TO US is not room noise -------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        bus = make_bus(tmp)
        cfg = pathlib.Path(tmp) / "cfg"; cfg.mkdir()
        seed(bus, [("peer-a", None, "broadcast noise"),
                   ("peer-b", "fresh-claude-2222", "TOKEN-ADDRESSED: read me"),
                   ("peer-c", "fresh-claude", "BARE-ADDRESSED: read me too"),
                   ("peer-d", "other-claude-9999", "for a different session")])
        r = run(bus, "fresh-claude-2222", cfg, "recv", "--summary")
        out = r.stdout
        check("THE CASE: directed messages survive the cold anchor",
              "2 pending" in out)
        check("  ... the TOKEN-addressed one is shown", "TOKEN-ADDRESSED" in out)
        check("  ... the BARE-name-addressed one is shown", "BARE-ADDRESSED" in out)
        check("CONTROL: a message for a DIFFERENT token is still not shown",
              "for a different session" not in out)
        check("CONTROL: broadcast is still skipped (not a history replay)",
              "broadcast noise" not in out)
        check("the counts are reported and add up (2 directed + 2 other = 4)",
              "4 historical" in out and "2 addressed to you" in out
              and "2 broadcast" in out)

    # ---- WARM CURSOR: the fix must not make ordinary verdicts noisy -------- #
    with tempfile.TemporaryDirectory() as tmp:
        bus = make_bus(tmp)
        cfg = pathlib.Path(tmp) / "cfg"; cfg.mkdir()
        seed(bus, [("peer-a", None, "one message")])
        run(bus, "warm-claude-3333", cfg, "recv", "--summary")       # anchor
        r = run(bus, "warm-claude-3333", cfg, "recv", "--summary")   # now warm
        out = r.stdout
        check("CONTROL: a warm cursor prints a CLEAN '0 pending', no suffix",
              "0 pending" in out and "cold anchor" not in out)
        check("CONTROL: and does not re-fire the fresh-identity notice",
              "cursor anchored at HEAD" not in out)

    # ---- EMPTY ROOM: no history at all is genuinely 0 pending -------------- #
    with tempfile.TemporaryDirectory() as tmp:
        bus = make_bus(tmp)
        cfg = pathlib.Path(tmp) / "cfg"; cfg.mkdir()
        seed(bus, [])
        r = run(bus, "fresh-claude-4444", cfg, "recv", "--summary")
        check("CONTROL: an empty room reports a clean 0 — the suffix is not "
              "unconditional decoration",
              "0 pending" in r.stdout and "cold anchor" not in r.stdout)

    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}")
        sys.exit(1)
    print("BATTERY: ALL GREEN. A cold cursor can no longer report an unread "
          "backlog as an empty one, and a message addressed to you is never "
          "skipped as room noise.")


if __name__ == "__main__":
    main()
