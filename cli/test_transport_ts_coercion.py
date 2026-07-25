"""test_transport_ts_coercion.py - regression battery for the 2026-07-25 bus read outage.

INCIDENT: one peer wrote ts="2026-07-25T14:31:21Z" (ISO-8601) and kind=None into a
1651-row room log. Message.from_jsonl did float(d.get("ts")) -> ValueError, raised out
of _read_file, which recv does not catch. Result: `recv` was DOWN for EVERY reader on
the bus, fleet-wide, from a single malformed field in a single well-formed JSON line.

That directly contradicted from_jsonl's own documented contract - "tolerant by design:
... only a JSON syntax error makes a line unparseable". This corpus locks the contract.

PRINCIPLE: a transport must degrade on a ROW, never on the LOG.
"""
import sys, pathlib, json
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import Message, _coerce_ts  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))


def main():
    print("[ts coercion - never raises]")
    check("epoch float passes through", _coerce_ts(1753452681.5) == 1753452681.5)
    check("epoch int becomes float", _coerce_ts(1753452681) == 1753452681.0)
    check("numeric STRING is accepted", _coerce_ts("1753452681") == 1753452681.0)
    check("None -> 0.0 (missing is not fatal)", _coerce_ts(None) == 0.0)

    # THE INCIDENT VALUE
    got = _coerce_ts("2026-07-25T14:31:21Z")
    check("THE INCIDENT: ISO-8601 with Z parses to epoch", got > 1.7e9, f"got={got}")
    check("ISO-8601 without Z also parses", _coerce_ts("2026-07-25T14:31:21") > 1.7e9)

    # known-clean negatives: garbage must degrade, not explode
    for junk in ("not a date", "", "   ", [], {}, object()):
        try:
            v = _coerce_ts(junk)
            ok = v == 0.0
        except Exception as e:
            ok = False
            v = f"RAISED {e}"
        check(f"garbage {type(junk).__name__} -> 0.0, never raises", ok, f"got={v}")

    print()
    print("[from_jsonl - the contract it documents]")
    line = json.dumps({"id": "d27cb207", "from": "termux-claude-d7d5a219", "to": "windows-claude",
                       "ts": "2026-07-25T14:31:21Z", "type": "msg", "body": "hi"})
    try:
        m = Message.from_jsonl(line)
        ok = True
    except Exception as e:
        m, ok = None, False
        print(f"      raised: {e}")
    check("the exact incident line parses instead of killing the read", ok)
    if m:
        check("kind=None does NOT become the string 'None'", m.kind == "msg", f"kind={m.kind!r}")
        check("body survives", m.body == "hi")
        check("from survives", m.from_ == "termux-claude-d7d5a219")

    # a whole log with one bad row must still read
    print()
    print("[log-level: one bad row must not take the log down]")
    rows = [json.dumps({"id": f"g{i}", "from": "peer", "ts": 1753452681 + i, "kind": "msg", "body": "ok"})
            for i in range(3)]
    rows.insert(1, line)
    parsed, died = [], None
    try:
        for r in rows:
            parsed.append(Message.from_jsonl(r))
    except Exception as e:
        died = e
    check("all 4 rows parse, none kills the batch", died is None and len(parsed) == 4,
          f"died={died} n={len(parsed)}")

    print()
    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}")
        return 1
    print("BATTERY: ALL GREEN. Transport degrades on a row, never on the log.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
