"""test_presence_lease_coercion.py — one malformed presence/lease file must not
take down the read for everyone else.

THE INCIDENT CLASS. 2026-07-25, a peer wrote ts="2026-07-25T14:31:21Z" into
chat.jsonl; `float(d.get("ts"))` raised ValueError out of _read_file and ONE bad
field took `recv` down for EVERY reader on the bus. `_coerce_ts` was written to
fix it — for the MESSAGE path only. Review 2026-09-04 found the two un-swept
siblings: _collect_presence and _collect_leases parsed ts / claimed_at / ttl with
a bare float() OUTSIDE their try (which catches JSONDecodeError and OSError, not
ValueError), so the same shape of file took down the whole presence read. That
matters more here than it did for chat: presence files are the most frequently
written objects on the bus, and the symptom is every box reading as OFFLINE.

Each arm below carries its CONTROL, because a guard that has only ever been fed
good input has been shown to run, not to work. The bad-file arms are the ones
that failed before the fix — verified by construction, on this file, first.

DIRECTION OF FAILURE is asserted, not just survival. The three fields fail in
three different directions and only one of them is "0.0":
  presence ts  -> 0.0  = maximally stale, identity stays VISIBLE (hiding a box
                         that is up would be absence-of-data rendered as data)
  lease ts     -> 0.0  = claim EXPIRED, work reads FREE (never falsely held)
  lease ttl    -> None = claim NOT alive. 0.0 would have meant "no expiry" —
                         a corrupt file becoming an IMMORTAL lease
  claimed_at   -> falls back to ts / now, never 0.0, which would win every
                         race by min(claimed_at)
"""
import json
import pathlib
import sys
import tempfile

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import FileBusTransport, _coerce_ttl  # noqa: E402

FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def bus(tmp):
    root = pathlib.Path(tmp)
    (root / ".securedchat-bus").write_text("test\n")
    t = FileBusTransport(root, "r", "reviewer")
    t.presence_dir.mkdir(parents=True, exist_ok=True)
    t.lease_dir.mkdir(parents=True, exist_ok=True)
    return t


def main():
    # ---- presence ----------------------------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        (t.presence_dir / "good.json").write_text(json.dumps({"identity": "good", "ts": 1788400000.0}))
        rows = t._collect_presence()
        check("CONTROL: a healthy presence file reads (the query CAN return non-zero)",
              [r["identity"] for r in rows] == ["good"])

        # THE CASE: well-formed JSON, ISO ts — the exact 2026-07-25 shape.
        (t.presence_dir / "bad.json").write_text(json.dumps({"identity": "bad", "ts": "2026-09-04T09:00:00Z"}))
        try:
            rows = t._collect_presence()
            crashed = False
        except Exception as e:  # noqa: BLE001 — any raise is the defect
            rows, crashed = [], True
            print(f"    (raised {type(e).__name__}: {e})")
        check("THE CASE: one ISO-ts presence file does NOT take the read down", not crashed)
        check("the HEALTHY identity survives the bad neighbour",
              any(r["identity"] == "good" and r["ts"] == 1788400000.0 for r in rows))
        bad = next((r for r in rows if r["identity"] == "bad"), None)
        check("the bad row stays VISIBLE (not hidden)", bad is not None)
        # _coerce_ts RECOVERS ISO-8601 rather than zeroing it — better than mere
        # survival, and the reason this arm asserts a real timestamp. (Written
        # first as `== 0.0`; the corpus failed it and the code was right.)
        check("an ISO ts is RECOVERED to its real epoch value, not discarded",
              bad is not None and bad["ts"] > 1e9)

        # Truly unreadable (not a number, not a date) — this is the 0.0 arm.
        (t.presence_dir / "junk.json").write_text(json.dumps({"identity": "junk", "ts": "banana"}))
        rows = t._collect_presence()
        junk = next((r for r in rows if r["identity"] == "junk"), None)
        check("DIRECTION: an unreadable presence ts reads as maximally stale, never fresh",
              junk is not None and junk["ts"] == 0.0 and junk["age"] > 1e9)

        # A ts that is a NUMERIC STRING is legitimate JSON from a sloppy writer
        # and must be read as the number, not coerced to 0.0.
        (t.presence_dir / "strnum.json").write_text(json.dumps({"identity": "strnum", "ts": "1788400001"}))
        rows = t._collect_presence()
        check("CONTROL: a numeric-string ts is parsed, not discarded",
              any(r["identity"] == "strnum" and r["ts"] == 1788400001.0 for r in rows))

    # ---- leases -------------------------------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        t = bus(tmp)
        import time
        now = time.time()
        (t.lease_dir / "w1__alice.json").write_text(json.dumps(
            {"work_id": "w1", "holder": "alice", "claimed_at": now - 10, "ts": now, "ttl": 600}))
        rows = t._collect_leases()
        check("CONTROL: a healthy lease reads and is alive",
              len(rows) == 1 and rows[0]["holder"] == "alice" and rows[0]["alive"])

        # THE CASE: bad ts on a DIFFERENT work_id must not kill the read.
        # The ISO string is generated from NOW, not hardcoded. Written first as a
        # literal "2026-09-04T09:00:00Z"; the arm asserts the lease is ALIVE, so a
        # fixed date meant the test passed only on the day it was written and went
        # red 17h later. A test whose verdict depends on the wall clock is the
        # clock-before-claim defect wearing a corpus.
        iso_now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))
        (t.lease_dir / "w2__bob.json").write_text(json.dumps(
            {"work_id": "w2", "holder": "bob", "ts": iso_now, "ttl": 600}))
        try:
            rows = t._collect_leases()
            crashed = False
        except Exception as e:  # noqa: BLE001
            rows, crashed = [], True
            print(f"    (raised {type(e).__name__}: {e})")
        check("THE CASE: one ISO-ts lease file does NOT take the read down", not crashed)
        check("the healthy lease survives the bad neighbour",
              any(r["work_id"] == "w1" and r["alive"] for r in rows))
        w2 = next((r for r in rows if r["work_id"] == "w2"), None)
        check("an ISO lease ts is RECOVERED (alive, since it is recent and inside ttl)",
              w2 is not None and w2["alive"] and w2["age"] < 1e9)

        # Truly unreadable ts — the safe-direction arm.
        (t.lease_dir / "w5__dave.json").write_text(json.dumps(
            {"work_id": "w5", "holder": "dave", "ts": "banana", "ttl": 600}))
        rows = t._collect_leases()
        w5 = next((r for r in rows if r["work_id"] == "w5"), None)
        check("DIRECTION: a lease with an unreadable ts reads EXPIRED (work free), never held",
              w5 is not None and not w5["alive"])

        # THE IMMORTAL-LEASE CASE: an unreadable ttl must not become 0.0, which
        # the aliveness test reads as "no expiry".
        (t.lease_dir / "w3__carol.json").write_text(json.dumps(
            {"work_id": "w3", "holder": "carol", "claimed_at": now, "ts": now, "ttl": "forever"}))
        rows = t._collect_leases()
        w3 = next((r for r in rows if r["work_id"] == "w3"), None)
        check("THE CASE: an unreadable ttl does NOT become an immortal lease",
              w3 is not None and not w3["alive"])
        check("CONTROL: a genuine ttl<=0 DOES still mean no-expiry",
              _coerce_ttl(0) == 0.0 and _coerce_ttl("bogus") is None and _coerce_ttl(600) == 600.0)

        # acquire_lease over our own corrupt prior claim must not raise, and must
        # not backdate us to the epoch.
        t2 = FileBusTransport(pathlib.Path(tmp), "r", "carol")
        (t2._lease_file("w4")).write_text(json.dumps(
            # fixed date is SAFE here: this arm asserts claimed_at is not
            # backdated to the epoch, which does not depend on how old it is.
            {"work_id": "w4", "holder": "carol", "claimed_at": "2026-09-04T09:00:00Z",
             "ts": now, "ttl": 600}))
        try:
            res = t2.acquire_lease("w4", ttl=600)
            crashed = False
        except Exception as e:  # noqa: BLE001
            res, crashed = {}, True
            print(f"    (raised {type(e).__name__}: {e})")
        check("THE CASE: acquiring over a corrupt prior claim does not raise", not crashed)
        check("it is treated as a renew, and claimed_at is NOT backdated to the epoch",
              res.get("status") == "renewed"
              and json.loads(t2._lease_file("w4").read_text())["claimed_at"] > 1e9)

    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}")
        sys.exit(1)
    print("BATTERY: ALL GREEN. One malformed presence/lease file degrades its own row, "
          "never the read — and each field fails in its safe direction.")


if __name__ == "__main__":
    main()
