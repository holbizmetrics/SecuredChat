"""test_send_lock_liveness.py — the send lock must break DEAD holders, never live ones.

THE DEFECT (review 2026-09-04, finding #2). _send_lock broke the lock on
`age > timeout OR now > deadline`, both fixed at 10s from acquisition, while a
single git call inside the critical section is bounded by GIT_TIMEOUT=120s and a
send makes several plus jittered push retries. A healthy holder doing a 45s push
therefore had its lock broken at 10s and a sibling ran git concurrently in the
same clone -- the exact failure the 2026-09-02 review bounded _git to prevent,
re-entered through the lock. Ten seconds of ELAPSED TIME was being read as death.

The fix makes the holder prove liveness (a daemon thread touching the lock file's
mtime every LOCK_HEARTBEAT), so `age` measures SILENCE, not duration.

Every arm carries its control, and the two that matter assert OPPOSITE outcomes
from the same waiter code -- a live holder is waited for, a dead one is broken.
A guard that only ever waits is a hang; one that only ever breaks is the bug.

NEGATIVE CONTROL, run separately: `test_live_holder_is_not_broken` FAILS against
git HEAD^ (the pre-fix transport.py). That is what makes the green here mean
something. See the commit message for the recorded before/after.

NOT COVERED HERE, said plainly: the Windows branches. _pid_alive returns None on
nt by construction (os.kill would terminate the peer), and the PermissionError
stale-break path is exercised by test_chat.py's mock, not by a real WinError 32.
Both need the Windows box.
"""
import os
import pathlib
import sys
import tempfile
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import transport  # noqa: E402
from transport import FileBusTransport, _pid_alive, _read_lock_holder  # noqa: E402

# Beat fast so the arms below run in about a second rather than in tens of them.
# LOCK_STALE_AFTER is floored at 3 heartbeats inside _send_lock, so shrinking the
# heartbeat is the ONLY way to test short budgets -- which is itself the point:
# a budget below a few beats cannot distinguish a slow holder from a dead one.
transport.LOCK_HEARTBEAT = 0.05

FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def bus(tmp, identity="alice"):
    root = pathlib.Path(tmp)
    (root / ".securedchat-bus").write_text("test\n")
    return FileBusTransport(root, "r", identity)


def test_live_holder_is_not_broken():
    """THE CASE. A holder that outlives `timeout` but keeps heartbeating is waited
    for, not broken. This is the arm that fails against the pre-fix code."""
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        lock_path = t.chat_file.parent / ".send.lock"
        hold_for = 1.2          # > stale_after below, i.e. "a slow but healthy push"
        released = threading.Event()

        def holder():
            with t._send_lock(timeout=0.4, max_wait=10):
                time.sleep(hold_for)
            released.set()

        th = threading.Thread(target=holder, daemon=True)
        th.start()
        time.sleep(0.15)
        check("CONTROL: the holder really is holding (lock file exists)", lock_path.exists())
        holder_rec = _read_lock_holder(lock_path)
        check("CONTROL: the lock names its holder (pid/host/identity)",
              holder_rec is not None and holder_rec["pid"] == os.getpid()
              and holder_rec["host"] == transport._HOST and holder_rec["identity"] == "alice")

        # A waiter with the SAME short staleness budget. Pre-fix this returned at
        # ~0.4s having broken a live lock; now it must wait for the real release.
        t2 = bus(tmp, "bob")
        t0 = time.time()
        with t2._send_lock(timeout=0.4, max_wait=10):
            waited = time.time() - t0
        check(f"THE CASE: a heartbeating holder is NOT broken (waiter blocked "
              f"{waited:.2f}s, holder held {hold_for}s)", waited >= hold_for - 0.25)
        check("the holder released normally rather than being stolen from",
              released.wait(2.0))
        check("the lock file is gone after both released", not lock_path.exists())


def test_dead_holder_is_broken_immediately():
    """The opposite verdict from the same code: a lock naming a dead pid on THIS
    host is broken at once -- faster than the old 10s, not slower."""
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        lock_path = t.chat_file.parent / ".send.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        # A pid that cannot be running: fork a child, reap it, reuse its pid.
        pid = os.fork()
        if pid == 0:
            os._exit(0)
        os.waitpid(pid, 0)
        check("CONTROL: the probe agrees the reaped pid is gone", _pid_alive(pid) is False)
        check("CONTROL: and agrees our OWN pid is alive (so it can answer both ways)",
              _pid_alive(os.getpid()) is True)

        lock_path.write_text(transport.json.dumps(
            {"pid": pid, "host": transport._HOST, "identity": "ghost", "op": "send-lock",
             "ts": time.time()}))
        os.utime(lock_path, None)   # FRESH mtime: age alone would say "hold off"
        t0 = time.time()
        with t._send_lock(timeout=30, max_wait=10):
            broke_in = time.time() - t0
        check(f"THE CASE: a dead holder is broken immediately despite a fresh "
              f"heartbeat and a 30s budget ({broke_in:.2f}s)", broke_in < 0.5)


def test_stale_heartbeat_is_broken():
    """A holder whose heartbeat STOPPED (wedged, or killed -9 on another host) is
    still broken by age -- the mtime path has to keep working."""
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        lock_path = t.chat_file.parent / ".send.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_path.write_text(transport.json.dumps(
            {"pid": os.getpid(), "host": "some-other-box", "identity": "far", "ts": 0}))
        old = time.time() - 1000
        os.utime(lock_path, (old, old))
        t0 = time.time()
        with t._send_lock(timeout=0.4, max_wait=10):
            broke_in = time.time() - t0
        check(f"a silent holder on ANOTHER host is broken by age ({broke_in:.2f}s)",
              broke_in < 0.5)

        # legacy/empty lock file (what the C# GitBusTransport writes) must still work
        lock_path.write_bytes(b"")
        os.utime(lock_path, (old, old))
        check("CONTROL: an empty lock file reads as 'holder unknown', not as a dict",
              _read_lock_holder(lock_path) is None)
        with t._send_lock(timeout=0.4, max_wait=10):
            pass
        check("a legacy/empty stale lock (C# holder format) is still broken by age", True)


def test_live_holder_past_max_wait_raises():
    """The old code never blocked forever; neither does this one. But it fails by
    RAISING rather than by breaking a live lock and running two gits in one clone."""
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        done = threading.Event()

        def holder():
            with t._send_lock(timeout=0.4, max_wait=10):
                done.wait(3.0)

        th = threading.Thread(target=holder, daemon=True)
        th.start()
        time.sleep(0.15)
        t2 = bus(tmp, "bob")
        raised = None
        t0 = time.time()
        try:
            with t2._send_lock(timeout=0.4, max_wait=0.6):
                pass
        except TimeoutError as e:
            raised = str(e)
        waited = time.time() - t0
        done.set()
        th.join(2.0)
        check(f"a live holder past max_wait raises instead of blocking forever "
              f"({waited:.2f}s)", raised is not None and waited < 2.0)
        check("the error NAMES the holder (pid/host/identity), not just 'timeout'",
              raised is not None and str(os.getpid()) in raised
              and transport._HOST in raised and "alice" in raised)
        check("CONTROL: it says the holder was alive and was NOT broken",
              raised is not None and "NOT broken" in raised)


def test_uncontended_is_not_slowed():
    """CONTROL for the whole file: none of the above may cost the common path."""
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        t0 = time.time()
        for _ in range(5):
            with t._send_lock():
                pass
        el = time.time() - t0
        check(f"5 uncontended acquire/release cycles stay fast ({el:.3f}s)", el < 0.5)
        check("no heartbeat thread survives release",
              not any(th.name == "send-lock-heartbeat" and th.is_alive()
                      for th in threading.enumerate()))


def main():
    print("test_send_lock_liveness")
    for fn in (test_live_holder_is_not_broken,
               test_dead_holder_is_broken_immediately,
               test_stale_heartbeat_is_broken,
               test_live_holder_past_max_wait_raises,
               test_uncontended_is_not_slowed):
        print(f" {fn.__name__}")
        fn()
    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}")
        sys.exit(1)
    print("BATTERY: ALL GREEN. The lock breaks dead holders instantly, waits on live "
          "ones, and raises rather than stealing from one that will not let go.")


if __name__ == "__main__":
    main()
