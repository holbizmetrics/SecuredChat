#!/usr/bin/env python3
"""Corpus lock for GitBusTransport._push_with_retry (bus presence-beat failure, 2026-08-31).

WHY THIS EXISTS. The live bus reported `presence push failed after retries` with
`! [rejected] main -> main (fetch first)`. The obvious reading -- "the remote was
unreachable" -- was wrong, and the measurement said so: 123 presence commits from
3 identities in 40 minutes, one box pushing 6 rooms inside ~10 seconds. Contention
on this bus is BURSTY. Three IMMEDIATE retries all land inside the same burst, so
the old loop was not unlucky, it was systematically retrying at the worst moment.

THE LOAD-BEARING CASE IS test_09/test_10. A corpus that only asserts "4 attempts
happen" locks in a constant. The pair simulates a 2.5s burst and asserts the OLD
configuration (3 attempts, no backoff) FAILS on it while the NEW one SUCCEEDS --
so if someone removes the backoff, the corpus goes red for the reason the backoff
exists rather than because a number changed.

SECOND DISCRIMINATING PAIR: test_05/test_06. A rejected push after a FAILED pull
is stale local state (fix the checkout); after a SUCCESSFUL pull it is a lost race
(wait and retry). The old loops discarded `_pull_rebase()`'s bool, so both surfaced
as the same text. The pair asserts the two messages differ.

No git, no network, no bus repo: the method is exercised against a stub that
records what it was asked to do. Plain script, matching test_presence_state.py.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from transport import GitBusTransport, PUSH_ATTEMPTS  # noqa: E402

FAILED = 0


def check(label, cond, detail=""):
    global FAILED
    if cond:
        print(f"PASS  {label}")
    else:
        FAILED += 1
        print(f"FAIL  {label}{(' -- ' + detail) if detail else ''}")


class _Result:
    def __init__(self, rc, stderr=""):
        self.returncode = rc
        self.stderr = stderr
        self.stdout = ""


class Stub:
    """Records pushes/pulls/sleeps. `push_ok_after` = attempt index that succeeds
    (None = never). `pull_ok` False simulates offline/conflict/no-upstream."""

    def __init__(self, push_ok_after=None, pull_ok=True, stderr="! [rejected] main -> main (fetch first)"):
        self.push_ok_after = push_ok_after
        self.pull_ok = pull_ok
        self.stderr = stderr
        self.pushes = 0
        self.pulls = 0
        self.sleeps = []

    def _git(self, *args, check=False):
        assert args[0] == "push", args
        self.pushes += 1
        ok = self.push_ok_after is not None and self.pushes > self.push_ok_after
        return _Result(0 if ok else 1, "" if ok else self.stderr)

    def _pull_rebase(self):
        self.pulls += 1
        return self.pull_ok

    def sleep(self, secs):
        self.sleeps.append(secs)


def run(stub, what="presence", attempts=PUSH_ATTEMPTS):
    """Returns the RuntimeError message, or None when the push succeeded."""
    try:
        GitBusTransport._push_with_retry(stub, what, attempts=attempts, sleep=stub.sleep)
        return None
    except RuntimeError as e:
        return str(e)


class BurstStub(Stub):
    """A peer holds the remote for `burst` seconds of SIMULATED time. A push
    succeeds only once the clock (advanced solely by our own backoff sleeps) has
    passed it -- which is exactly the question: does this retry policy wait long
    enough to outlast a burst, or does it spend all its attempts inside one?"""

    def __init__(self, burst):
        super().__init__(push_ok_after=None)
        self.burst = burst
        self.clock = 0.0

    def _git(self, *args, check=False):
        assert args[0] == "push", args
        self.pushes += 1
        if self.clock >= self.burst:
            return _Result(0)
        return _Result(1, "! [rejected] main -> main (fetch first)")

    def sleep(self, secs):
        self.sleeps.append(secs)
        self.clock += secs


def main():
    # --- the happy path costs nothing extra -------------------------------
    s = Stub(push_ok_after=0)
    check("test_01 first push succeeds -> returns without raising", run(s) is None)
    check("test_02 a successful first push costs 0 sleeps and 0 pulls",
          s.pushes == 1 and s.pulls == 0 and s.sleeps == [],
          f"pushes={s.pushes} pulls={s.pulls} sleeps={s.sleeps}")

    # --- recovery ---------------------------------------------------------
    s = Stub(push_ok_after=1)
    check("test_03 a lost race then a win does NOT raise", run(s) is None)
    check("test_04 recovery re-syncs exactly once between the two pushes",
          s.pushes == 2 and s.pulls == 1 and len(s.sleeps) == 1,
          f"pushes={s.pushes} pulls={s.pulls} sleeps={s.sleeps}")

    # --- THE DISCRIMINATING PAIR: cause is carried, not discarded ---------
    lost_race = run(Stub(push_ok_after=None, pull_ok=True))
    stale = run(Stub(push_ok_after=None, pull_ok=False))
    check("test_05 exhausting attempts with HEALTHY pulls raises without blaming local state",
          lost_race is not None and "STALE LOCAL STATE" not in lost_race, lost_race)
    check("test_06 NEGATIVE CONTROL a FAILED pull is named as stale state, not a lost race",
          stale is not None and "STALE LOCAL STATE" in stale, stale)
    check("test_07 the two failures are distinguishable at all (the old loop's defect)",
          lost_race != stale)

    # --- the push's own stderr survives into the message ------------------
    msg = run(Stub(push_ok_after=None, stderr="fatal: could not read Username"))
    check("test_08 the underlying git error is carried, not swallowed",
          msg is not None and "could not read Username" in msg, msg)

    # --- THE LOAD-BEARING PAIR: backoff is what clears a burst ------------
    old = BurstStub(burst=2.5)
    old_msg = run(old, attempts=3)
    # Reproduce the ORIGINAL configuration: 3 attempts is not the whole story --
    # what killed it was that they were immediate. Zero-length sleeps model that.
    immediate = BurstStub(burst=2.5)
    immediate.sleep = lambda secs: immediate.sleeps.append(0.0)
    imm_msg = run(immediate, attempts=3)
    check("test_09 NEGATIVE CONTROL 3 IMMEDIATE retries lose to a 2.5s burst",
          imm_msg is not None and immediate.clock == 0.0,
          f"msg={imm_msg} clock={immediate.clock}")
    new = BurstStub(burst=2.5)
    check("test_10 the shipped config (backoff, %d attempts) OUTLASTS the same burst" % PUSH_ATTEMPTS,
          run(new) is None, f"clock={new.clock} sleeps={new.sleeps}")
    check("test_11 and it did so by WAITING, not by pushing harder",
          new.clock >= 2.5 and new.pushes <= PUSH_ATTEMPTS,
          f"clock={new.clock} pushes={new.pushes}")
    # test_12 (3 backed-off vs 3 immediate on the same burst) was WRITTEN AND
    # REMOVED: its outcome straddles the burst length (2 backoffs span 1.2-3.6s
    # against a 2.5s burst), so it passes or fails on the jitter draw. A flaky
    # check teaches the next reader to re-run until green, which is worse than
    # the coverage it buys. test_09/test_10 already lock the property.
    check("test_12 the immediate-retry control genuinely never advanced its clock",
          immediate.clock == 0.0 and old.clock > 0.0,
          f"immediate={immediate.clock} backed_off={old.clock}")

    # --- shape guarantees -------------------------------------------------
    s = Stub(push_ok_after=None)
    run(s)
    check("test_13 exhaustion spends exactly `attempts` pushes",
          s.pushes == PUSH_ATTEMPTS, f"pushes={s.pushes}")
    check("test_14 no pull after the FINAL attempt (nothing would read it)",
          s.pulls == PUSH_ATTEMPTS - 1, f"pulls={s.pulls}")
    check("test_15 backoff is strictly increasing (exponential, not a flat retry)",
          all(b < a for b, a in zip(s.sleeps, s.sleeps[1:])), f"sleeps={s.sleeps}")
    check("test_16 backoff is JITTERED, not a constant ladder",
          _jitter_differs(), "20 runs produced identical sleep ladders")

    # --- the caller's label reaches the operator --------------------------
    check("test_17 the label names WHICH push failed",
          "lease release" in (run(Stub(push_ok_after=None), what="lease release") or ""))
    check("test_18 a different label produces a different message",
          run(Stub(push_ok_after=None), what="presence") != run(Stub(push_ok_after=None), what="archive"))

    print(f"\n{18 - FAILED}/18 checks passed" if not FAILED else f"\n{FAILED} of 18 FAILED")
    return 1 if FAILED else 0


def _jitter_differs():
    """Two exhausted runs must not produce byte-identical sleep ladders -- if they
    do, the jitter was removed and two contending clones re-collide in lockstep."""
    for _ in range(20):
        a, b = Stub(push_ok_after=None), Stub(push_ok_after=None)
        run(a), run(b)
        if a.sleeps != b.sleeps:
            return True
    return False


if __name__ == "__main__":
    sys.exit(main())
