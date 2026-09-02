"""test_archive_replay.py - a row re-appended with an ARCHIVED id must not come back as new on
the fast path (review 2026-09-02). compact() writes archive/ids.txt; the fast path drops matches."""
import sys, pathlib, tempfile, subprocess, io, contextlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import GitBusTransport, Message  # noqa: E402
import time, uuid
def mk(i): return Message(ts=time.time() + i, id=str(uuid.uuid4()), from_="alice", to=None, kind="msg", body=f"m{i}")

FAILS = []
def check(name, cond, detail=""):
    if not cond: FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp); subprocess.run(["git", "init", "-q", str(root)], check=True)
        subprocess.run(["git", "-C", str(root), "config", "user.email", "t@t"], check=True); subprocess.run(["git", "-C", str(root), "config", "user.name", "t"], check=True)
        (root / ".securedchat-bus").write_text("x")
        t = GitBusTransport(root, "r", "alice")
        with contextlib.redirect_stderr(io.StringIO()):
            ids = []
            for i in range(5):
                m = mk(i); t.send(m); ids.append(m.id)
            n = t.compact(keep_last=2)
        check("compact archived 3 of 5", n == 3, f"n={n}")
        idx = root / "r" / "archive" / "ids.txt"
        check("archive/ids.txt written with the 3 archived ids", idx.exists() and set(idx.read_text().split()) == set(ids[:3]))
        # THE ATTACK: re-append the archived row 0 (same id) to the active file
        archived_row = [m for seg in t._archive_segments() for m in t._read_file(seg) if m.id == ids[0]][0]
        with (root / "r" / "chat.jsonl").open("a", encoding="utf-8") as f: f.write(archived_row.to_jsonl() + "\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fast = t._recv_resolved(ids[3])   # full-length cursor in the active tail -> fast path
        got = [m.id for m in fast]
        check("THE ATTACK: replayed archived id is NOT returned on the fast path", ids[0] not in got, f"got={[g[:8] for g in got]}")
        check("THE ATTACK: a REPLAY line names the dropped id", "REPLAY dropped" in err.getvalue() and ids[0][:8] in err.getvalue())
        check("CONTROL: the genuinely newer row after the cursor is still returned", got == [ids[4]], f"got={[g[:8] for g in got]}")
        # CONTROL: full-history path also delivers each id at most once (oldest copy wins)
        full = [m.id for m in t._recv_resolved(None)]
        check("CONTROL: full-history read has no duplicate ids", len(full) == len(set(full)) and ids[0] in full)
        # CONTROL: without an index (pre-guard archive), nothing is filtered and the reader is told
        idx.unlink(); err = io.StringIO()
        with contextlib.redirect_stderr(err):
            fast2 = [m.id for m in t._recv_resolved(ids[3])]
        check("CONTROL: no index -> guard inactive, said once, replay passes (documented)", ids[0] in fast2 and "archive index missing" in err.getvalue())
    if FAILS: print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}"); sys.exit(1)
    print("BATTERY: ALL GREEN. An archived id re-appended to the tail is a REPLAY, dropped and named.")
if __name__ == "__main__": main()
