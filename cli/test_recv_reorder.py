"""test_recv_reorder.py - the cursor must not lose a row that a merge put BEFORE it.

REVIEW 2026-09-02: a stranded local row M (push exhausted) is rebased on top of fetched
history; merge=union emits the fetched N before M; a cursor on M never returns N.
Reproduced by construction in a two-clone repo before this battery was written.
"""
import sys, pathlib, json, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import LocalJsonlBus, Message  # noqa: E402

class _T(LocalJsonlBus):  # the base class is abstract; the cursor logic under test lives on it
    def send(self, *a, **k): raise NotImplementedError
    def recv(self, since_id=None): return self._recv_resolved(since_id)

FAILS = []
def check(name, cond, detail=""):
    if not cond: FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
def row(i, ts): return json.dumps({"ts": ts, "id": f"{i}" * 36, "from": "x", "to": None, "kind": "msg", "body": i})
def bus(tmp, lines):
    root = pathlib.Path(tmp); (root / "r").mkdir(exist_ok=True); (root / ".securedchat-bus").write_text("x")
    (root / "r" / "chat.jsonl").write_text("\n".join(lines) + "\n")
    return _T(root, "r", "me")
def main():
    M, N = "m", "n"
    with tempfile.TemporaryDirectory() as tmp:
        # THE INCIDENT SHAPE: file order [1, 2, N, M] with ts(N) > ts(M); cursor on M
        t = bus(tmp, [row("1", 10.0), row("2", 11.0), row(N, 13.0), row(M, 12.0)])
        got = [m.body for m in t._recv_resolved(M * 36)]
        check("THE INCIDENT: N reordered before the cursor IS returned", got == [N], f"got={got}")
        # CONTROL: ordinary order [1, 2, M, N] -> N returned (unchanged behaviour)
        t = bus(tmp, [row("1", 10.0), row("2", 11.0), row(M, 12.0), row(N, 13.0)])
        got = [m.body for m in t._recv_resolved(M * 36)]
        check("CONTROL: normal order still returns the tail", got == [N], f"got={got}")
        # CONTROL: an OLDER row before the cursor is NOT re-delivered (no duplicates)
        t = bus(tmp, [row("1", 10.0), row("2", 11.0), row(M, 12.0)])
        got = [m.body for m in t._recv_resolved(M * 36)]
        check("CONTROL: rows older than the cursor are not replayed", got == [], f"got={got}")
        # both: reordered N AND a normal tail row T -> N first (ts order), then T
        t = bus(tmp, [row("1", 10.0), row(N, 13.0), row(M, 12.0), row("t", 14.0)])
        got = [m.body for m in t._recv_resolved(M * 36)]
        check("reordered row delivered ahead of the tail, tail kept", got == [N, "t"], f"got={got}")
        # short-prefix (full-history) path takes the same rule
        got = [m.body for m in t._recv_resolved(M * 8)]
        check("short-prefix cursor path applies the same recovery", got == [N, "t"], f"got={got}")
        # HONEST RESIDUAL made explicit: a reordered row with ts <= cursor is still missed
        t = bus(tmp, [row("1", 10.0), row(N, 12.0), row(M, 12.0)])
        got = [m.body for m in t._recv_resolved(M * 36)]
        check("RESIDUAL (documented): reordered row with ts <= cursor is NOT recovered", got == [], f"got={got}")
    if FAILS: print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}"); sys.exit(1)
    print("BATTERY: ALL GREEN. A merge-reordered row is delivered, not skipped; nothing older is replayed.")
if __name__ == "__main__": main()
