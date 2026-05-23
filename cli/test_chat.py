#!/usr/bin/env python3
"""Tests for the SecuredChat CLI hardening pass.

Covers the four structural findings fixed in chat.py / transport.py:
  #1 cursor scoped per (identity, room) + legacy-global read fallback
  #2 from-vs-git-author verification (warn keeps, strict drops, unverifiable kept)
  #3 recv takes the repo lock (smoke: recv still works under the lock)
  #4 archive-aware reads + compact roundtrip (no loss) + cursor fast-path

Self-contained: builds throwaway git repos in a temp dir, no remote, no pytest.
Run:  python test_chat.py    (exit 0 = all pass, 1 = a failure)
"""
from __future__ import annotations

import json
import queue
import shutil
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import chat  # noqa: E402
from transport import (  # noqa: E402
    BUS_MARKER, FileBusTransport, GitBusTransport, Message, WebRTCTransport,
)

_failures: list[str] = []
_passed = 0


def check(cond: bool, label: str) -> None:
    global _passed
    if cond:
        _passed += 1
        print(f"  PASS  {label}")
    else:
        _failures.append(label)
        print(f"  FAIL  {label}")


def _git(repo: Path, *args: str, **kw: str) -> subprocess.CompletedProcess:
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=True, **kw)


def make_bus(root: Path, name: str) -> Path:
    repo = root / name
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "test@securedchat")
    _git(repo, "config", "user.name", "test")
    (repo / BUS_MARKER).write_text("test bus\n")
    _git(repo, "add", BUS_MARKER)
    _git(repo, "commit", "-q", "-m", "init bus")
    return repo


def send(t, from_: str, body: str, to: str | None = None) -> Message:
    m = Message.new(from_=from_, to=to, body=body)
    t.send(m)
    return m


def make_file_bus(root: Path, name: str) -> Path:
    """A plain (NON-git) bus directory for the file transport."""
    d = root / name
    d.mkdir(parents=True)
    (d / BUS_MARKER).write_text("test file bus\n")
    return d


# --------------------------------------------------------------------------- #
def test_cursor_scoping(root: Path) -> None:
    print("test_cursor_scoping (R1: scoped only, no blanket legacy inheritance)")
    cfg = root / "cfghome" / ".config" / "securedchat"
    # Redirect chat.py's cursor globals at module level (used by the helpers).
    chat.CONFIG_DIR = cfg
    chat.LEGACY_LAST_SEEN_FILE = cfg / "last-seen-id"
    chat.CURSOR_DIR = cfg / "cursors"

    # No cursor anywhere yet.
    check(chat._read_last_seen("windows-claude", "relay") is None, "absent scoped cursor -> None")

    # R1: a legacy global cursor is NOT blanket-inherited by _read_last_seen.
    chat.LEGACY_LAST_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    chat.LEGACY_LAST_SEEN_FILE.write_text("legacy-id\n")
    check(chat._read_last_seen("windows-claude", "relay") is None,
          "scoped read does NOT inherit the legacy global (R1 fix)")

    # Scoped cursor reads back; different identities/rooms are independent.
    chat._write_last_seen("windows-claude", "relay", "scoped-A")
    check(chat._read_last_seen("windows-claude", "relay") == "scoped-A", "scoped cursor read back")
    chat._write_last_seen("phone-claude", "relay", "scoped-B")
    check(chat._read_last_seen("windows-claude", "relay") == "scoped-A",
          "windows cursor unaffected by phone mark-seen (no clobber)")
    check(chat._read_last_seen("phone-claude", "relay") == "scoped-B", "phone cursor independent")
    check(chat._read_last_seen("windows-claude", "other-room") is None,
          "new room -> None, NOT a stranger's legacy cursor (R1 fix)")

    # Sanitization keeps exotic names inside CURSOR_DIR.
    p = chat._cursor_file("a/b..c", "r m")
    check(chat.CURSOR_DIR in p.parents, "exotic identity/room stays under CURSOR_DIR")


def test_resolve_since_migration(root: Path) -> None:
    print("test_resolve_since_migration (R1: resolve-checked legacy adoption)")
    cfg = root / "cfghome2" / ".config" / "securedchat"
    chat.CONFIG_DIR = cfg
    chat.LEGACY_LAST_SEEN_FILE = cfg / "last-seen-id"
    chat.CURSOR_DIR = cfg / "cursors"

    repo = make_bus(root, "bus_migrate")
    t = GitBusTransport(repo, "relay", "alice")
    msgs = [send(t, "alice", f"m{i}") for i in range(3)]

    # No scoped cursor, no legacy -> None (full history).
    check(chat._resolve_since(t, "alice", "relay") is None, "no cursor + no legacy -> None")

    # A legacy id that does NOT resolve in this room is NOT inherited (R1).
    chat.LEGACY_LAST_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    chat.LEGACY_LAST_SEEN_FILE.write_text("0" * 36 + "\n")
    check(chat._resolve_since(t, "alice", "relay") is None,
          "unresolvable legacy id is NOT inherited")

    # A legacy id that DOES resolve here is adopted + persisted scoped (migration).
    chat.LEGACY_LAST_SEEN_FILE.write_text(msgs[0].id + "\n")
    check(chat._resolve_since(t, "alice", "relay") == msgs[0].id, "resolvable legacy id adopted")
    check(chat._read_last_seen("alice", "relay") == msgs[0].id,
          "adopted legacy persisted as scoped cursor")


def test_recv_since_and_fastpath(root: Path) -> None:
    print("test_recv_since_and_fastpath")
    repo = make_bus(root, "bus_recv")
    t = GitBusTransport(repo, "relay", "alice")
    msgs = [send(t, "alice", f"m{i}") for i in range(6)]

    got = t.recv(since_id=None)
    check([m.id for m in got] == [m.id for m in msgs], "recv(None) returns all in order")

    after2 = t.recv(since_id=msgs[2].id)  # full uuid -> fast path
    check([m.id for m in after2] == [m.id for m in msgs[3:]], "recv(full id) returns tail")

    # Short prefix (full-history path) resolves the same tail.
    after2p = t.recv(since_id=msgs[2].id[:8])
    check([m.id for m in after2p] == [m.id for m in msgs[3:]], "recv(prefix) returns tail")

    # Stale full-length cursor -> nothing (no backlog replay).
    bogus = "0" * 36
    check(t.recv(since_id=bogus) == [], "stale full-length cursor -> []")


def test_compact_roundtrip(root: Path) -> None:
    print("test_compact_roundtrip")
    repo = make_bus(root, "bus_compact")
    t = GitBusTransport(repo, "relay", "alice")
    msgs = [send(t, "alice", f"m{i}") for i in range(10)]

    n = t.compact(keep_last=3)
    check(n == 7, "compact archived 7 of 10")

    active_lines = (repo / "relay" / "chat.jsonl").read_text().splitlines()
    check(len(active_lines) == 3, "active file holds only kept tail (3)")
    check((repo / "relay" / "archive").is_dir(), "archive dir created")

    allm = t.recv(since_id=None)
    check([m.id for m in allm] == [m.id for m in msgs], "recv(None) returns all 10 after compact (no loss, in order)")

    # Cursor pointing at an ARCHIVED message still resolves (full-history path).
    after_arch = t.recv(since_id=msgs[2].id)
    check([m.id for m in after_arch] == [m.id for m in msgs[3:]],
          "archived cursor spans archive->active correctly")

    # Cursor in the active tail uses the fast path and returns the right tail.
    after_kept = t.recv(since_id=msgs[8].id)
    check([m.id for m in after_kept] == [msgs[9].id], "kept-tail cursor fast path")

    # recv --id equivalent finds an archived message.
    full = t.recv(since_id=None)
    found = [m for m in full if m.id.startswith(msgs[1].id[:8])]
    check(len(found) == 1 and found[0].body == "m1", "recv --id finds archived message")

    # Second compaction segment sorts after the first (chronological stitching).
    for i in range(10, 14):
        send(t, "alice", f"m{i}")
    t.compact(keep_last=2)
    allm2 = t.recv(since_id=None)
    check([m.body for m in allm2] == [f"m{i}" for i in range(14)],
          "two compactions preserve full chronological order")


def test_from_verification(root: Path) -> None:
    print("test_from_verification")
    repo = make_bus(root, "bus_verify")
    t = GitBusTransport(repo, "relay", "alice")

    legit = send(t, "alice", "legit")  # commit author == alice == from

    # Spoof: from=alice but committed by mallory.
    spoof = Message.new(from_="alice", to=None, body="spoofed")
    chat_file = repo / "relay" / "chat.jsonl"
    with chat_file.open("a", encoding="utf-8") as f:
        f.write(spoof.to_jsonl() + "\n")
    _git(repo, "add", "relay/chat.jsonl")
    _git(repo, "-c", "user.name=mallory", "-c", "user.email=mallory@x",
         "commit", "-q", "-m", f"chat: relay {spoof.id[:8]}")

    # Unverifiable: a line whose commit subject doesn't follow the chat pattern.
    unk = Message.new(from_="alice", to=None, body="manual")
    with chat_file.open("a", encoding="utf-8") as f:
        f.write(unk.to_jsonl() + "\n")
    _git(repo, "add", "relay/chat.jsonl")
    _git(repo, "-c", "user.name=bob", "-c", "user.email=bob@x",
         "commit", "-q", "-m", "manual edit not following pattern")

    amap = t.commit_author_map()
    check(amap.get(legit.id[:8]) == "alice", "author map: legit -> alice")
    check(amap.get(spoof.id[:8]) == "mallory", "author map: spoof -> mallory")
    check(unk.id[:8] not in amap, "author map: unverifiable absent")

    allm = t.recv(since_id=None)
    warn = chat._verify_from(t, allm, strict=False)
    check([m.id for m in warn] == [m.id for m in allm], "warn keeps all (incl spoof + unverifiable)")

    strict = chat._verify_from(t, allm, strict=True)
    ids = {m.id for m in strict}
    check(legit.id in ids and unk.id in ids and spoof.id not in ids,
          "strict drops spoof, keeps legit + unverifiable")


def test_gitattributes(root: Path) -> None:
    print("test_gitattributes (H1: merge=union driver)")
    repo = make_bus(root, "bus_ga")
    t = GitBusTransport(repo, "relay", "alice")
    send(t, "alice", "first")  # send ensures + commits .gitattributes

    ga = repo / ".gitattributes"
    text = ga.read_text(encoding="utf-8") if ga.exists() else ""
    check("chat.jsonl merge=union" in text, ".gitattributes has chat.jsonl merge=union")
    check("chat-*.jsonl merge=union" in text, ".gitattributes has chat-*.jsonl merge=union")
    tracked = _git(repo, "ls-files").stdout.split()
    check(".gitattributes" in tracked, ".gitattributes is committed/tracked")
    check(t._ensure_gitattributes() is False, "_ensure_gitattributes idempotent (no rewrite)")


def test_id_resolves_and_dedup(root: Path) -> None:
    print("test_id_resolves_and_dedup (A1 helper + read dedup)")
    repo = make_bus(root, "bus_dedup")
    t = GitBusTransport(repo, "relay", "alice")
    msgs = [send(t, "alice", f"m{i}") for i in range(4)]
    t.compact(keep_last=2)  # archive m0,m1 ; keep m2,m3

    check(t._id_resolves(msgs[0].id) is True, "_id_resolves True for archived id")
    check(t._id_resolves("0" * 36) is False, "_id_resolves False for stale id")

    # Force a duplicate: append an archived message's line back into the active
    # file (simulates a union-merge of compaction vs concurrent append).
    chat_file = repo / "relay" / "chat.jsonl"
    with chat_file.open("a", encoding="utf-8") as f:
        f.write(msgs[0].to_jsonl() + "\n")
    _git(repo, "add", "relay/chat.jsonl")
    _git(repo, "commit", "-q", "-m", "simulate dup line")

    got = t.recv(since_id=None)
    ids = [m.id for m in got]
    check(ids == [m.id for m in msgs], "duplicate line across archive+active collapses to one (dedup)")
    check(len(ids) == len(set(ids)), "no duplicate ids returned")


def test_watch_reanchor(root: Path) -> None:
    print("test_watch_reanchor (A1: watch survives a stale cursor)")
    repo = make_bus(root, "bus_watch")
    t = GitBusTransport(repo, "relay", "alice")
    for i in range(3):
        send(t, "alice", f"m{i}")

    out: "queue.Queue[Message]" = queue.Queue()

    def run():
        try:
            for m in t.watch(poll_seconds=0.2, since_id="0" * 36):  # start STALE
                out.put(m)
                return  # one emission is enough to prove re-anchor
        except Exception:
            pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.6)  # let it detect stale + re-anchor to head
    new = send(t, "bob", "after-reanchor")
    try:
        m = out.get(timeout=4)
        check(m.id == new.id, "watch re-anchored on stale cursor and emitted the new message")
    except queue.Empty:
        check(False, "watch re-anchored on stale cursor and emitted the new message (timed out)")
    th.join(timeout=1)


def test_bus_monitor(root: Path) -> None:
    print("test_bus_monitor (Claude-session background watcher)")
    repo = make_bus(root, "bus_mon")
    t = GitBusTransport(repo, "relay", "alice")
    send(t, "alice", "before-monitor", to="bob")  # baseline: must NOT be replayed

    proc = subprocess.Popen(
        [sys.executable, str(HERE / "bus_monitor.py"),
         "--bus", str(repo), "--room", "relay", "--identity", "bob", "--poll", "0.2"],
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        time.sleep(1.2)                      # start + anchor to head + MONITOR_READY
        new = send(t, "alice", "hello-bob", to="bob")
        own = send(t, "bob", "my own echo")  # from self -> must be filtered out
        time.sleep(1.5)                      # let it poll (0.2s) and emit
    finally:
        proc.terminate()
        try:
            out, _ = proc.communicate(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
            out, _ = proc.communicate()

    check("MONITOR_READY" in out, "monitor emits MONITOR_READY at startup")
    check(new.id[:8] in out and "BUS_MSG" in out, "monitor emits BUS_MSG for a new addressed message")
    check("before-monitor" not in out, "monitor anchors to head (no backlog replay)")
    check(own.id[:8] not in out, "monitor excludes my own messages by default")


def test_presence(root: Path) -> None:
    print("test_presence (liveness heartbeat)")
    repo = make_bus(root, "bus_presence")
    ta = GitBusTransport(repo, "relay", "alice")
    tb = GitBusTransport(repo, "relay", "bob")
    ta.announce_presence()
    tb.announce_presence()

    rows = ta.read_presence()
    idents = {r["identity"] for r in rows}
    check(idents == {"alice", "bob"}, "presence lists all announced identities")
    check(all(r["age"] < 60 for r in rows), "fresh presence has small age")

    # Re-announce alice → still exactly one alice record (overwrite, not append).
    ta.announce_presence()
    rows2 = ta.read_presence()
    check(sum(1 for r in rows2 if r["identity"] == "alice") == 1,
          "presence is one-file-per-identity (overwritten, not appended)")

    # Presence must not pollute the chat log.
    chat = repo / "relay" / "chat.jsonl"
    check((not chat.exists()) or "presence" not in chat.read_text(encoding="utf-8"),
          "presence does not pollute chat.jsonl")

    # A presence file exists per identity under presence/.
    pdir = repo / "relay" / "presence"
    check(pdir.is_dir() and (pdir / "alice.json").exists() and (pdir / "bob.json").exists(),
          "presence/<identity>.json files created")


def test_leases(root: Path) -> None:
    print("test_leases (task claim/lease coordination, git transport)")
    repo = make_bus(root, "bus_leases")
    ta = GitBusTransport(repo, "relay", "alice")
    tb = GitBusTransport(repo, "relay", "bob")

    r = ta.acquire_lease("work-1", ttl=60)
    check(r["status"] == "acquired" and r["holder"] == "alice", "first claimer acquires")

    r = tb.acquire_lease("work-1", ttl=60)
    check(r["status"] == "conflict" and r["holder"] == "alice",
          "second claimer is refused and sees the holder")

    lf = repo / "relay" / "leases" / "work-1__alice.json"
    first = json.loads(lf.read_text(encoding="utf-8"))
    time.sleep(0.02)
    r = ta.acquire_lease("work-1", ttl=60)
    second = json.loads(lf.read_text(encoding="utf-8"))
    check(r["status"] == "renewed", "holder re-claim renews")
    check(second["claimed_at"] == first["claimed_at"] and second["ts"] >= first["ts"],
          "renew preserves claimed_at, advances ts")
    check(len(list((repo / "relay" / "leases").glob("work-1__*.json"))) == 1,
          "one lease file per (work-id, identity) — overwritten, not appended")

    rows = ta.read_leases()
    held = [x for x in rows if x["work_id"] == "work-1"]
    check(len(held) == 1 and held[0]["holder"] == "alice" and held[0]["alive"],
          "read_leases resolves the holder")

    r = tb.acquire_lease("work-2", ttl=60)
    check(r["status"] == "acquired" and r["holder"] == "bob", "a different work-id is independent")

    ta.acquire_lease("work-3", ttl=0.05)
    time.sleep(0.1)
    check(ta._resolve_holder("work-3") is None, "expired lease (age>ttl) frees the work-id")
    r = tb.acquire_lease("work-3", ttl=60)
    check(r["status"] == "acquired" and r["holder"] == "bob", "expired lease is reclaimable by another")

    r = ta.release_lease("work-1")
    check(r["status"] == "released", "holder can release")
    r = tb.acquire_lease("work-1", ttl=60)
    check(r["status"] == "acquired" and r["holder"] == "bob", "released lease is reclaimable")

    chatf = repo / "relay" / "chat.jsonl"
    check((not chatf.exists()) or "lease" not in chatf.read_text(encoding="utf-8"),
          "leases do not pollute chat.jsonl")


def test_leases_file(root: Path) -> None:
    print("test_leases_file (b1 leases, no git)")
    busdir = make_file_bus(root, "filebus_leases")
    ta = FileBusTransport(busdir, "relay", "alice")
    tb = FileBusTransport(busdir, "relay", "bob")
    check(ta.acquire_lease("w", ttl=60)["status"] == "acquired", "file: first claimer acquires")
    check(tb.acquire_lease("w", ttl=60)["status"] == "conflict", "file: second claimer refused")
    check(ta.release_lease("w")["status"] == "released", "file: holder releases")
    check(tb.acquire_lease("w", ttl=60)["status"] == "acquired", "file: reclaim after release")
    check(not (busdir / ".git").exists(), "file leases create no .git")


def test_delivery_ack(root: Path) -> None:
    print("test_delivery_ack (kind=ack receipts + delivered query)")
    repo = make_bus(root, "bus_ack")
    ta = GitBusTransport(repo, "relay", "alice")
    tb = GitBusTransport(repo, "relay", "bob")
    tc = GitBusTransport(repo, "relay", "carol")

    m = send(ta, "alice", "please process X")  # broadcast
    # bob + carol acknowledge: kind=ack, reply_to=m.id (what `ack` / `recv --ack` emit)
    tb.send(Message.new(from_="bob", to="alice", body="", kind="ack", reply_to=m.id))
    tc.send(Message.new(from_="carol", to="alice", body="", kind="ack", reply_to=m.id))

    allm = ta.recv(since_id=None)
    acks = [x for x in allm if x.kind == "ack" and (x.reply_to or "") == m.id]
    check({x.from_ for x in acks} == {"bob", "carol"},
          "delivered: both receipts attributed to the right message")
    one = acks[0]
    check(one.kind == "ack" and one.reply_to == m.id and one.body == "",
          "ack message shape (kind=ack, reply_to set, empty body)")

    m2 = send(ta, "alice", "second")
    acks2 = [x for x in ta.recv(since_id=None) if x.kind == "ack" and (x.reply_to or "") == m2.id]
    check(acks2 == [], "delivered: an un-acked message has no receipts")


def test_identity_validation(root: Path) -> None:
    print("test_identity_validation (R4: reject metachars in identity/room)")
    repo = make_bus(root, "bus_valid")
    ok = GitBusTransport(repo, "relay", "windows-claude")
    check(ok.identity == "windows-claude", "valid identity accepted")
    for bad in ["bad room", "a\nb", "x=y", "../escape", ""]:
        try:
            GitBusTransport(repo, "relay", bad)
            check(False, f"invalid identity {bad!r} should be rejected")
        except RuntimeError:
            check(True, f"invalid identity {bad!r} rejected")
    try:
        GitBusTransport(repo, "bad room", "alice")
        check(False, "invalid room should be rejected")
    except RuntimeError:
        check(True, "invalid room rejected")


def test_file_transport(root: Path) -> None:
    print("test_file_transport (b1: shared-dir JSONL, no git, no server)")
    busdir = make_file_bus(root, "filebus")
    ta = FileBusTransport(busdir, "relay", "alice")
    tb = FileBusTransport(busdir, "relay", "bob")

    check(not (busdir / ".git").exists(), "file transport creates no .git (truly gitless)")

    msgs = [send(ta, "alice", f"m{i}") for i in range(5)]
    got = tb.recv(since_id=None)
    check([m.id for m in got] == [m.id for m in msgs], "file recv(None) returns all in order")

    # Two identities exchange through the same directory.
    b = send(tb, "bob", "hi alice", to="alice")
    seen_by_alice = ta.recv(since_id=None)
    check(seen_by_alice[-1].id == b.id and seen_by_alice[-1].from_ == "bob",
          "second identity's message visible to the first via the shared dir")

    # Cursor semantics: full-length fast path, short prefix, stale -> [].
    after2 = tb.recv(since_id=msgs[2].id)
    check([m.id for m in after2] == [m.id for m in msgs[3:]] + [b.id], "file recv(full id) returns tail")
    after2p = tb.recv(since_id=msgs[2].id[:8])
    check([m.id for m in after2p] == [m.id for m in msgs[3:]] + [b.id], "file recv(prefix) returns tail")
    check(tb.recv(since_id="0" * 36) == [], "file stale full-length cursor -> [] (no backlog replay)")

    check(ta.last_pull_ok is True, "file transport last_pull_ok stays True (no remote to be stale against)")

    # Compact roundtrip: archive, no loss, order preserved, cursor still spans.
    n = ta.compact(keep_last=2)
    check(n == 4, "file compact archived 4 of 6")
    active_lines = (busdir / "relay" / "chat.jsonl").read_text().splitlines()
    check(len(active_lines) == 2, "file active holds only kept tail (2)")
    check((busdir / "relay" / "archive").is_dir(), "file archive dir created")
    allm = tb.recv(since_id=None)
    check([m.id for m in allm] == [m.id for m in msgs] + [b.id], "file recv(None) intact after compact (no loss)")
    after_arch = tb.recv(since_id=msgs[1].id)
    check([m.id for m in after_arch] == [m.id for m in msgs[2:]] + [b.id],
          "file archived cursor spans archive->active")

    # Presence (overwrite-per-identity, no commit, no chat pollution).
    ta.announce_presence()
    tb.announce_presence()
    rows = ta.read_presence()
    check({r["identity"] for r in rows} == {"alice", "bob"}, "file presence lists all identities")
    ta.announce_presence()
    rows2 = ta.read_presence()
    check(sum(1 for r in rows2 if r["identity"] == "alice") == 1, "file presence one-file-per-identity")

    # verify-from degrades gracefully: no commits -> empty map -> keep all.
    check(ta.commit_author_map() == {}, "file commit_author_map empty (nothing to verify)")
    kept = chat._verify_from(ta, allm, strict=True)
    check([m.id for m in kept] == [m.id for m in allm], "file verify-from strict keeps all (unverifiable)")

    # watch (shared poll loop) emits a newly appended message past the anchor.
    out: "queue.Queue[Message]" = queue.Queue()
    tw = FileBusTransport(busdir, "relay", "carol")
    anchor = allm[-1].id

    def run():
        try:
            for m in tw.watch(poll_seconds=0.2, since_id=anchor):
                out.put(m)
                return
        except Exception:
            pass

    th = threading.Thread(target=run, daemon=True)
    th.start()
    time.sleep(0.4)
    newm = send(ta, "alice", "live-after-watch")
    try:
        emitted = out.get(timeout=4)
        check(emitted.id == newm.id, "file watch emits a newly appended message")
    except queue.Empty:
        check(False, "file watch emits a newly appended message (timed out)")
    th.join(timeout=1)

    # Identity/room validation inherited from the shared base.
    for bad in ["bad room", "x=y", "../escape", ""]:
        try:
            FileBusTransport(busdir, "relay", bad)
            check(False, f"file: invalid identity {bad!r} should be rejected")
        except RuntimeError:
            check(True, f"file: invalid identity {bad!r} rejected")


def test_webrtc_signaling_guard(root: Path) -> None:
    print("test_webrtc_signaling_guard (b2: malformed SDP frames skipped; no aiortc needed)")
    import asyncio
    sigdir = make_file_bus(root, "wsig2")
    ta = WebRTCTransport(FileBusTransport(sigdir, "relay", "alice"),
                         "relay", "alice", state_dir=root / "wg")
    sb = FileBusTransport(sigdir, "relay", "bob")
    # A rogue/garbled, a well-addressed-but-malformed, then a valid answer.
    sb.send(Message.new("bob", "alice", "not json at all", kind=WebRTCTransport.SIGNAL_ANSWER))
    sb.send(Message.new("bob", "alice", json.dumps({"foo": 1}), kind=WebRTCTransport.SIGNAL_ANSWER))
    sb.send(Message.new("bob", "alice", json.dumps({"sdp": "v=0", "type": "answer"}),
                        kind=WebRTCTransport.SIGNAL_ANSWER))
    desc = asyncio.run(ta._await_signal(WebRTCTransport.SIGNAL_ANSWER, "bob", timeout=5))
    check(desc == {"sdp": "v=0", "type": "answer"},
          "await_signal skips malformed frames and returns the valid SDP")
    # A frame from the wrong sender is ignored (only `peer` is accepted).
    sc = FileBusTransport(sigdir, "relay", "carol")
    sc.send(Message.new("carol", "alice", json.dumps({"sdp": "x", "type": "answer"}),
                        kind=WebRTCTransport.SIGNAL_ANSWER))
    ta2 = WebRTCTransport(FileBusTransport(sigdir, "relay", "alice"),
                          "relay", "alice", state_dir=root / "wg2")
    try:
        asyncio.run(ta2._await_signal(WebRTCTransport.SIGNAL_ANSWER, "dave", timeout=2))
        check(False, "await_signal should time out when no frame from the named peer")
    except TimeoutError:
        check(True, "await_signal ignores frames from other senders (times out for an absent peer)")


def test_webrtc_loopback(root: Path) -> None:
    try:
        import aiortc  # noqa: F401
    except ImportError:
        print("test_webrtc_loopback (b2): SKIP — aiortc not installed "
              "(`pip install aiortc` to exercise the data channel)")
        return
    print("test_webrtc_loopback (b2: data channel over loopback, file signaling)")
    sigdir = make_file_bus(root, "wsig")
    sa = FileBusTransport(sigdir, "relay", "alice")
    sb = FileBusTransport(sigdir, "relay", "bob")
    ta = WebRTCTransport(sa, "relay", "alice", state_dir=root / "wa", ice_servers=[])
    tb = WebRTCTransport(sb, "relay", "bob", state_dir=root / "wb", ice_servers=[])
    errs: dict[str, Exception] = {}

    def offer() -> None:
        try:
            ta.connect("bob", "offer", timeout=30)
        except Exception as e:  # noqa: BLE001
            errs["offer"] = e

    def answer() -> None:
        try:
            tb.connect("alice", "answer", timeout=30)
        except Exception as e:  # noqa: BLE001
            errs["answer"] = e

    th2 = threading.Thread(target=answer, daemon=True)
    th1 = threading.Thread(target=offer, daemon=True)
    th2.start()
    time.sleep(0.2)
    th1.start()
    th1.join(35)
    th2.join(35)
    check(not errs, f"both peers established the data channel (errs={errs})")
    if errs:
        ta.close()
        tb.close()
        return

    m = Message.new(from_="alice", to="bob", body="live p2p hello")
    ta.send(m)
    delivered = False
    for _ in range(80):
        if any(x.id == m.id for x in tb.recv(since_id=None)):
            delivered = True
            break
        time.sleep(0.1)
    check(delivered, "message delivered peer-to-peer over the data channel")

    # The signaling bus carried ONLY the handshake — proves the live message
    # never touched git/the bus (git is out of the hot path).
    sig_msgs = sa.recv(since_id=None)
    kinds = {sm.kind for sm in sig_msgs}
    check(kinds <= {"sdp-offer", "sdp-answer"},
          f"signaling bus carried only handshake frames ({kinds})")
    check(all("live p2p hello" not in sm.body for sm in sig_msgs),
          "live message never written to the signaling bus")

    ta.close()
    tb.close()


def _rm(path: Path) -> None:
    def onerr(func, p, exc):
        try:
            Path(p).chmod(stat.S_IWRITE)
            func(p)
        except Exception:
            pass
    shutil.rmtree(path, onerror=onerr)


def main() -> int:
    root = Path(tempfile.mkdtemp(prefix="securedchat-test-"))
    try:
        test_cursor_scoping(root)
        test_resolve_since_migration(root)
        test_recv_since_and_fastpath(root)
        test_compact_roundtrip(root)
        test_from_verification(root)
        test_gitattributes(root)
        test_id_resolves_and_dedup(root)
        test_watch_reanchor(root)
        test_bus_monitor(root)
        test_presence(root)
        test_leases(root)
        test_leases_file(root)
        test_delivery_ack(root)
        test_identity_validation(root)
        test_file_transport(root)
        test_webrtc_signaling_guard(root)
        test_webrtc_loopback(root)
    finally:
        _rm(root)
    print()
    if _failures:
        print(f"FAILED: {len(_failures)} / {_passed + len(_failures)}")
        for f in _failures:
            print(f"  - {f}")
        return 1
    print(f"OK: {_passed} checks passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
