"""Transport layer for SecuredChat CLI.

Defines a Transport abstraction so the CLI can swap out delivery mechanisms
without changing the user-facing commands.

  - GitBusTransport  — append-only JSONL in a git repo; push/pull is the sync
                       primitive. Cross-machine, durable, store-and-forward.
  - FileBusTransport — the same append-only JSONL, but in a plain shared/synced
                       directory (NAS, Syncthing, or just same-machine). No git,
                       no server. Same-host writers are serialized by a lock;
                       cross-host sync is whatever the filesystem provides.
  - (planned) WebRTCTransport — aiortc data channel, SDP handshake bootstrapped
                       over a bus; real-time, peer-to-peer, DTLS-encrypted.

The shared, transport-agnostic machinery (reading the JSONL log + archive
segments, cursor/prefix resolution, the poll-based `watch`, the same-host send
lock, presence file reads) lives in LocalJsonlBus; each concrete transport adds
only its own delivery/sync.

Message wire format (JSONL line):

    {"ts": <unix-float>, "id": <uuid>, "from": <str>, "to": <str-or-null>,
     "kind": "msg", "body": <str>, "reply_to": <id-or-absent>,
     "sig": <armored-ssh-signature-or-absent>, "sig_alg": <tag-or-absent>}

`to: null` = broadcast within the room. `kind` reserved for future control
frames (sdp-offer, sdp-answer, presence, etc.). `reply_to` is the id of the
message this one answers (threading); absent when not a reply. `sig`/`sig_alg`
are the optional per-message signature (leg 3) — absent on unsigned messages;
the signature covers the content tuple, NOT itself (see signing.py). The
`sig_alg` tag lets a future signing backend be added without a wire break.

Parsing is deliberately tolerant: unknown keys are ignored (a newer peer may
add fields) and missing keys are defaulted (an older peer may omit them), so a
schema change on one side never makes the other silently drop messages.
"""
from __future__ import annotations

import json
import os
import random
import re
import subprocess
import sys
import time
import uuid
from abc import ABC, abstractmethod
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator

# Marker file that identifies a repo/dir as a dedicated SecuredChat bus (never a
# code repo). `init` creates it; the transport warns if it is missing.
BUS_MARKER = ".securedchat-bus"

# Identity/room must be safe for git author/subject AND filenames — reject
# anything that could inject into `git -c user.name=...`, a commit subject, or a
# path (newline, '=', '/', '..', control chars).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# Push contention on a shared bus is BURSTY, not uniform (see _push_with_retry).
# 4 attempts with jittered exponential backoff span ~1-8s of contention; the old
# 3 immediate retries spanned only the round-trip time and so retried INTO the
# same burst. Raise the ceiling here, not at five call sites.
PUSH_ATTEMPTS = 4
PUSH_BACKOFF_MIN = 0.4
PUSH_BACKOFF_MAX = 1.2
GIT_TIMEOUT = float(os.environ.get("SECUREDCHAT_GIT_TIMEOUT", "120"))  # seconds; every _git call


_TS_WARNED: set = set()   # dedupe the degradation warning per process


def _coerce_ts(value, _row_id=None) -> float:
    """Epoch seconds from whatever a peer wrote. NEVER raises.

    from_jsonl documents itself as "tolerant by design: ... only a JSON syntax error
    makes a line unparseable". `float(d.get("ts"))` broke that contract: a WELL-FORMED
    line whose ts is an ISO-8601 string raised ValueError out of _read_file, which recv
    does not catch -- so ONE malformed field from ONE peer took `recv` down for EVERY
    reader on the bus, fleet-wide, until someone edited the log.

    Incident 2026-07-25: termux-claude-d7d5a219 wrote ts="2026-07-25T14:31:21Z" (and
    kind=None, which str() turned into the literal string "None"). 1651 good rows, one
    bad field, total read outage.

    A transport must degrade on a row, never on the log. Unparseable timestamp -> 0.0,
    which sorts the message to the start rather than hiding or killing it.

    HONEST RESIDUAL — this is the RECEIVER half, and it is the weaker half.
    termux-claude-d7d5a219, repairing its own bad row, put it better than I did:
    *a writer must never invent a schema it could have read off the line above it.*
    The decidable sender-side form, for any hand-written append to a shared log:
    parse the last good line, assert your key SET equals its key set and each
    value's TYPE matches, then write. Two lines.

    The asymmetry matters: THIS fix makes readers SURVIVE bad writers; only the
    sender check PREVENTS them. You need both, and nothing here enforces the
    second — no writer is obliged to call it. Naming the gap so the next person
    does not read receiver-tolerance as the whole solution.
    """
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip()
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    try:  # ISO-8601, with or without a trailing Z
        from datetime import datetime, timezone
        iso = s[:-1] + "+00:00" if s.endswith("Z") else s
        dt = datetime.fromisoformat(iso)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        pass
    # REPORT the degradation. Guarding alone lets survival be mistaken for health:
    # a row whose ts silently became 0.0 sorts to the front of every listing forever
    # and nobody is ever told why. termux-claude-d7d5a219 flagged this as the missing
    # second half of the fix-shape -- guard at the boundary so the container survives
    # the element, AND say so, or the container is quietly wrong instead of loudly
    # degraded. Deduplicated per process so a log with one old bad row does not spam
    # every read; stderr so it never contaminates parsed stdout.
    key = str(_row_id or "")[:8] or repr(value)[:40]
    if key not in _TS_WARNED:
        _TS_WARNED.add(key)
        try:
            print(f"securedchat: WARNING row {key} has an unreadable ts "
                  f"({value!r}); using 0.0 -- it will sort to the start.",
                  file=sys.stderr)
        except Exception:
            pass
    return 0.0


def _is_git_noise(line: str) -> bool:
    """True for git fetch/rebase output lines whose FAULT content is zero: the
    "From <url>" header and successful per-ref status lines ("* branch main ->
    FETCH_HEAD", "abc123..def456  main -> origin/main", "= [up to date] ...").
    Their length scales with the REMOTE URL / ref names, which is what made a
    fixed msg[:200] cap strip the fault's identity (SELF-DISGUISE defect,
    courier row 124): boilerplate ate ~102-107 chars before the fault began.
    Rejection markers ("! [rejected] ... -> ...") ARE faults and are kept."""
    if line.startswith("From "):
        return True
    if "->" not in line or line.startswith("!"):
        return False
    low = line.lower()
    return not any(k in low for k in
                   ("error", "fatal", "warning", "hint", "cannot", "conflict"))


def _summarize_git_failure(stderr: str | None, stdout: str | None,
                           limit: int = 300) -> str:
    """One-line summary of a failed git op that KEEPS the fault's identity.

    Rule it enforces (the fix for SELF-DISGUISE): the fault text must survive
    regardless of how long the remote URL or ref boilerplate is, and any
    truncation must announce itself — a silently capped reason is the
    absence-of-data-rendered-as-data class. Pure: no I/O, fully testable."""
    text = ((stderr or "") + "\n" + (stdout or "")).strip()
    if not text:
        return "no output captured"
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    kept = [ln for ln in lines if not _is_git_noise(ln)]
    # If everything matched the noise filter, the "noise" was the whole story —
    # fall back to all lines rather than report nothing.
    msg = " | ".join(kept if kept else lines)
    if len(msg) > limit:
        msg = f"{msg[:limit]}… [+{len(msg) - limit} more chars]"
    return msg


def _coerce_opt_str(value, field: str = "", _row_id=None):
    """None stays None; a str passes through; ANYTHING ELSE becomes its str() form.
    NEVER raises. Review 2026-09-02 re-opened the _coerce_ts class on four more fields:
    from_jsonl passed `to`, `reply_to`, `sig`, `sig_alg`, `sig_v` through raw, so one row
    with "to": 5 raised TypeError inside _addressed_to (identity.startswith(to + "-")) for
    EVERY reader running `recv --addressed-to-me`, and "sig": 1 raised inside verify under
    any --verify-sig policy. `main` catches only RuntimeError/OSError -> traceback, fleet-wide
    read outage from one line, until someone edits the log.
    Coercing to str (not to None) is deliberate for `to`: a non-string target must never
    silently become a BROADCAST -- "5" addresses nobody, which is the safe failure."""
    if value is None or isinstance(value, str):
        return value
    return str(value)


def _coerce_opt_int(value):
    """For sig_v, which is declared int|None and COMPARED as an int downstream
    (canonical_payload: `sig_v == 2` selects the bound payload, anything not in
    {1,2} raises SigningError). The 2026-09-02 typed-fields commit routed sig_v
    through _coerce_opt_str, so a v2 row came back as "2" and silently stopped
    matching -- test_chat's wire-roundtrip check caught it on merge (termux).
    None stays None; ints pass; an int-looking str parses; anything else -> None,
    which reads as legacy/absent -- fail-closed under require_v2 (LEGACY_SIG ->
    dropped), never a fabricated version. NEVER raises."""
    if value is None or (isinstance(value, int) and not isinstance(value, bool)):
        return value
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _coerce_ttl(value):
    """Lease TTL in seconds, or None when it cannot be read. NEVER raises.

    SEPARATE from _coerce_ts on purpose, because the SAFE FAILURE DIRECTION is the
    opposite one. `_collect_leases` reads `ttl <= 0` as "no expiry", so coercing an
    unreadable ttl to 0.0 would turn a corrupt lease file into an IMMORTAL lease that
    blocks its work_id for every peer, forever. None instead means "this claim cannot
    be honoured", and the aliveness test below treats it as expired -> the work reads
    FREE. Two claimants racing is a condition the earliest-claimed_at rule already
    resolves; a permanently held lock is not.
    """
    if value is None:
        return None
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return None


@dataclass
class Message:
    ts: float
    id: str
    from_: str
    to: str | None
    kind: str
    body: str
    reply_to: str | None = None
    sig: str | None = None       # armored SSH signature over canonical_payload (leg 3)
    sig_alg: str | None = None   # signature scheme tag, e.g. "ssh"; absent when unsigned
    sig_v: int | None = None     # canonical_payload version: absent/1 legacy, 2 = room/bus-bound

    @classmethod
    def new(cls, from_: str, to: str | None, body: str, kind: str = "msg",
            reply_to: str | None = None) -> "Message":
        return cls(
            ts=time.time(),
            id=str(uuid.uuid4()),
            from_=from_,
            to=to,
            kind=kind,
            body=body,
            reply_to=reply_to,
        )

    def to_jsonl(self) -> str:
        d = {
            "ts": self.ts,
            "id": self.id,
            "from": self.from_,
            "to": self.to,
            "kind": self.kind,
            "body": self.body,
        }
        if self.reply_to is not None:
            d["reply_to"] = self.reply_to
        if self.sig is not None:
            d["sig"] = self.sig
            d["sig_alg"] = self.sig_alg or "ssh"
            if self.sig_v:
                d["sig_v"] = self.sig_v
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> "Message":
        # Tolerant by design: ignore unknown keys, default missing ones.
        # Only a JSON syntax error makes a line unparseable (handled by recv).
        d = json.loads(line)
        from_ = d.get("from", d.get("from_", ""))
        return cls(
            ts=_coerce_ts(d.get("ts"), _row_id=d.get("id")),
            id=str(d.get("id", "")),
            from_=str(from_),
            to=_coerce_opt_str(d.get("to"), "to", d.get("id")),
            kind=str(d.get("kind") or "msg"),
            body=str(d.get("body", "")),
            reply_to=_coerce_opt_str(d.get("reply_to"), "reply_to", d.get("id")),
            sig=_coerce_opt_str(d.get("sig"), "sig", d.get("id")),
            sig_alg=_coerce_opt_str(d.get("sig_alg"), "sig_alg", d.get("id")),
            sig_v=_coerce_opt_int(d.get("sig_v")),
        )


class Transport(ABC):
    @abstractmethod
    def send(self, msg: Message) -> None: ...

    @abstractmethod
    def recv(self, since_id: str | None = None) -> list[Message]: ...

    @abstractmethod
    def watch(self, poll_seconds: float = 5.0, since_id: str | None = None) -> Iterator[Message]: ...


class LocalJsonlBus(Transport):
    """Shared base for transports backed by an append-only JSONL log on a local
    path (a git repo, or a plain shared/synced directory).

    Holds everything that is independent of *how* the log syncs: reading the
    active file + archive segments, cursor/prefix resolution, the poll-based
    `watch` (with stale-cursor re-anchoring), the same-host advisory send lock,
    and presence-file reads. Concrete subclasses implement `send` (+ their own
    sync) and `recv` (calling `_recv_resolved` after they've synced).

    Layout under <root>:
        <room>/chat.jsonl              active tail (recent messages)
        <room>/archive/chat-*.jsonl    compacted older segments (oldest first)
        <room>/presence/<id>.json      one overwritten file per identity
    """

    def __init__(self, root: Path, room: str, identity: str):
        if not _SAFE_NAME.match(room or ""):
            raise RuntimeError(f"invalid room {room!r}: use only letters, digits, . _ -")
        if not _SAFE_NAME.match(identity or ""):
            raise RuntimeError(f"invalid identity {identity!r}: use only letters, digits, . _ -")
        self.root = Path(root).resolve()
        self.room = room
        self.identity = identity
        # True unless a sync (git pull) failed; file transports have no remote so
        # local reads are always "fresh" and this stays True. recv consumers read
        # it to tell "0 pending" apart from "offline / stale".
        self.last_pull_ok = True
        self.chat_file = self.root / room / "chat.jsonl"
        # Compaction moves old messages here as chat-<seq>.jsonl segments; the
        # active chat.jsonl keeps only the recent tail. _read_all stitches
        # archive segments + active back together so history is never lost.
        self.archive_dir = self.chat_file.parent / "archive"

    # ----- log reading (transport-agnostic) ------------------------------- #

    @property
    def _archive_index(self) -> Path:
        return self.archive_dir / "ids.txt"

    def _archive_ids(self):
        """Set of archived ids from archive/ids.txt, or None if no index exists."""
        try:
            if not self._archive_index.exists():
                return None
            return {ln.strip() for ln in self._archive_index.read_text(encoding="utf-8").splitlines() if ln.strip()}
        except OSError:
            return None

    def _write_archive_index(self, seg_path: Path, to_archive: list) -> None:
        """archive/ids.txt: the fast path's replay guard. Seeded from every existing segment
        the first time (one-off, at a moment that already rewrites the room), then appended
        per compaction. Shared by every transport that compacts."""
        idx = self._archive_index
        if not idx.exists():
            seed = [m.id for seg in self._archive_segments() if seg != seg_path for m in self._read_file(seg)]
            idx.write_text("".join(i + "\n" for i in seed), encoding="utf-8")
        with idx.open("a", encoding="utf-8") as f:
            for m in to_archive:
                f.write(m.id + "\n")

    def _archive_segments(self) -> list[Path]:
        """Archive segments in chronological order (lexicographic = chronological
        because segment names are zero-padded / timestamped at compaction)."""
        if not self.archive_dir.is_dir():
            return []
        return sorted(self.archive_dir.glob("chat-*.jsonl"))

    def _read_file(self, path: Path) -> list[Message]:
        if not path.exists():
            return []
        msgs: list[Message] = []
        with path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = Message.from_jsonl(line)
                except json.JSONDecodeError:
                    print(
                        f"securedchat: skipping unparseable line: {line[:80]}",
                        file=sys.stderr,
                    )
                    continue
                if not m.id:
                    print(
                        f"securedchat: skipping message with no id: {line[:80]}",
                        file=sys.stderr,
                    )
                    continue
                msgs.append(m)
        return msgs

    def _read_all(self, include_archive: bool = True) -> list[Message]:
        """Read messages in chronological order.

        include_archive=True  → archive segments (oldest first) + active tail
                                 = the full history (needed for full dumps,
                                 `recv --id`, and resolving an old/archived cursor).
        include_archive=False → active chat.jsonl only (the recent tail) — the
                                 fast path for a recent cursor that resolves
                                 within the active file.
        """
        msgs: list[Message] = []
        if include_archive:
            for seg in self._archive_segments():
                msgs.extend(self._read_file(seg))
            msgs.extend(self._read_file(self.chat_file))
        else:
            # FAST PATH REPLAY GUARD (review 2026-09-02): this path reads the active
            # tail only, so a row re-appended with an ARCHIVED id (same id, same valid
            # signature) came back as new and verified. The full-history path was safe
            # (dedup keeps the oldest copy); this one had no memory. compact() now
            # maintains archive/ids.txt; when it exists, an active row whose id is in
            # it is a REPLAY, dropped and named. When it does not exist yet (archives
            # older than this guard), nothing is filtered and the reader is told once.
            archived = self._archive_ids()
            active = self._read_file(self.chat_file)
            if archived is None:
                if self._archive_segments():
                    print("securedchat: archive index missing (pre-2026-09 archive) -- fast-path "
                          "replay guard inactive until the next `compact`", file=sys.stderr)
                msgs.extend(active)
            else:
                replays = [m for m in active if m.id in archived]
                if replays:
                    print(f"securedchat: REPLAY dropped -- {len(replays)} active row(s) carry an "
                          f"ARCHIVED id: " + ", ".join(m.id[:8] for m in replays), file=sys.stderr)
                msgs.extend(m for m in active if m.id not in archived)
        # Dedup by id, keeping the first (oldest) copy. Guards against a line that
        # ends up in BOTH an archive segment and the active file — e.g. a
        # union-merge of a compaction (which removed lines) racing a concurrent
        # append (which re-added them). recv must deliver each id at most once.
        seen: set[str] = set()
        deduped: list[Message] = []
        for m in msgs:
            if m.id in seen:
                continue
            seen.add(m.id)
            deduped.append(m)
        return deduped

    def _id_resolves(self, since_id: str) -> bool:
        """True iff `since_id` uniquely matches a message currently in the log
        (full history). Used by watch to tell a stale cursor (re-anchor) apart
        from a valid cursor with simply no new messages (keep waiting)."""
        matches = [m for m in self._read_all(include_archive=True) if m.id.startswith(since_id)]
        return len(matches) == 1

    def _recv_resolved(self, since_id: str | None) -> list[Message]:
        """Resolve `since_id` against the log and return the messages after it.
        Assumes the caller has already synced (pull) and is holding the lock if
        the transport needs one.

        Fast path: a full-length cursor (mark-seen writes the full id) that lands
        in the active tail means everything after it is also in the active tail —
        skip reading archive segments. Short `--since` prefixes take the
        full-history path to preserve exact prefix / ambiguity semantics.

        A stale/ambiguous cursor returns NOTHING (with a warning) rather than
        silently replaying the whole backlog.
        """
        if since_id is not None and len(since_id) >= 32:
            active = self._read_all(include_archive=False)
            hits = [i for i, m in enumerate(active) if m.id.startswith(since_id)]
            if len(hits) == 1:
                return self._after_cursor(active, hits[0])
            # 0 hits → cursor is archived or stale; >1 → ambiguous in active.
            # Both fall through to full-history resolution below.
        msgs = self._read_all(include_archive=True)
        if since_id is None:
            return msgs
        matches = [i for i, m in enumerate(msgs) if m.id.startswith(since_id)]
        if not matches:
            print(
                f"securedchat: since-id {since_id[:12]!r} not found — returning nothing "
                f"(stale cursor; not replaying backlog)",
                file=sys.stderr,
            )
            return []
        if len(matches) > 1:
            print(
                f"securedchat: since-id {since_id[:12]!r} is ambiguous "
                f"({len(matches)} matches) — returning nothing",
                file=sys.stderr,
            )
            return []
        return self._after_cursor(msgs, matches[0])

    @staticmethod
    def _after_cursor(msgs: list, i: int) -> list:
        """Everything after the cursor, PLUS rows that sit BEFORE it out of order.

        Review 2026-09-02 (linux-claude-5534b575), reproduced by construction: a send
        whose push exhausts leaves its row M committed locally; the next pull-rebase
        applies M on top of the fetched history, and `merge=union` emits the fetched
        line N FIRST, then M. A cursor already on M (watch, mark-seen, fresh-anchor)
        then never returns N -- a positional slice after M is empty. N is lost to that
        reader for good, and nothing says so.

        Rows are appended in ts order by every writer, so a row that sits before the
        cursor with a ts GREATER than the cursor's can only be there by merge reorder
        after the cursor was set. Those are delivered here, in ts order, ahead of the
        tail, with a stderr note naming the mechanism.

        HONEST RESIDUAL: a reordered row whose ts is <= the cursor's (the writer's
        clock ran a few seconds behind ours) is indistinguishable from an already-
        delivered row and is still missed. Exact recovery needs a seen-set beside the
        cursor, which is a cursor-format change and not done here."""
        cur = msgs[i]
        late = [m for m in msgs[:i] if m.ts > cur.ts]
        if late:
            print(
                f"securedchat: {len(late)} message(s) landed BEFORE cursor {cur.id[:8]} by "
                f"merge reorder (ts newer than the cursor) -- delivered now, not skipped: "
                + ", ".join(m.id[:8] for m in late),
                file=sys.stderr,
            )
        return sorted(late, key=lambda m: m.ts) + msgs[i + 1:]

    # ----- same-host send lock (transport-agnostic) ----------------------- #

    @contextmanager
    def _send_lock(self, timeout: float = 10.0):
        """Best-effort advisory lock serializing log-mutating ops on ONE machine.

        Held by `send` (sync + append + commit) AND by `recv` (its sync + file
        read). Without it, a same-machine recv could sync while a send is mid-
        write, or read a half-written line. Cross-machine concurrency is covered
        by the sync layer (git rebase-retry) or the shared filesystem. If the
        lock can't be acquired within `timeout` (e.g. a crashed holder left a
        stale lock), the stale lock is broken and we proceed rather than block.
        """
        lock_path = self.chat_file.parent / ".send.lock"
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        deadline = time.time() + timeout
        fd = None
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > timeout or time.time() > deadline:
                    try:
                        lock_path.unlink(missing_ok=True)  # break stale lock, then retry
                    except PermissionError:
                        # Windows: unlink fails with WinError 32 while another
                        # process holds the file open. POSIX unlink succeeds on
                        # open files; Windows doesn't. A PermissionError here
                        # means the holder is alive — the lock isn't actually
                        # stale. Back off and retry like ordinary contention.
                        time.sleep(0.1)
                    continue
                time.sleep(0.1)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
                lock_path.unlink(missing_ok=True)

    # ----- watch (transport-agnostic, drives recv) ------------------------ #

    def watch(self, poll_seconds: float = 5.0, since_id: str | None = None) -> Iterator[Message]:
        last_id: str | None = since_id
        seen: set[str] = set()
        seen_order: list[str] = []
        while True:
            batch = self.recv(since_id=last_id)
            for m in batch:
                if m.id in seen:
                    continue
                seen.add(m.id)
                seen_order.append(m.id)
                if len(seen_order) > 2000:  # bound memory on long-running watch
                    seen.discard(seen_order.pop(0))
                last_id = m.id
                yield m
            # A1: if a poll yielded nothing AND the cursor no longer resolves in
            # the (freshly pulled) log, it is STALE — re-anchor to head and resume
            # from new messages. Otherwise recv(since=stale) returns [] forever and
            # the watcher is permanently dead, never emitting even brand-new
            # messages. Distinct from "valid cursor, simply nothing new" (which
            # resolves and must keep waiting, not re-anchor + replay).
            if not batch and last_id is not None and not self._id_resolves(last_id):
                head = self._read_all(include_archive=True)
                new_anchor = head[-1].id if head else None
                if new_anchor != last_id:
                    print(
                        "securedchat: watch cursor stale — re-anchored to head; "
                        "resuming from new messages only.",
                        file=sys.stderr,
                    )
                    last_id = new_anchor
            time.sleep(poll_seconds)

    # ----- presence reads (transport-agnostic) ---------------------------- #

    @property
    def presence_dir(self) -> Path:
        return self.chat_file.parent / "presence"

    def _collect_presence(self) -> list[dict]:
        """Return [{identity, ts, age}] for every presence file, newest first.
        `age` is seconds since that identity's last heartbeat. Callers apply
        their own online/stale window. Pure file read — no sync.

        SIBLING SWEEP of the 2026-07-25 _coerce_ts incident (review 2026-09-04,
        reproduced by construction before fixing): this read used a bare
        `float(d.get("ts"))` OUTSIDE the try, which catches only JSONDecodeError
        and OSError. A WELL-FORMED presence file whose ts is an ISO string
        (`"2026-09-04T09:00:00Z"` — the exact shape one peer already wrote to
        chat.jsonl in that incident) raised ValueError out of this function, so
        ONE bad file made EVERY identity unreadable, fleet-wide, and the symptom
        was the whole bus reading as offline. _coerce_ts was written for exactly
        this class and applied only to the message path; presence and leases were
        the un-swept siblings. Presence files are the most frequently written
        objects on the bus, so this was the likeliest place for it to fire.

        Direction of failure, deliberately: an unreadable ts becomes 0.0, which
        reads as maximally STALE and keeps the identity VISIBLE. Dropping the row
        instead would hide a box that is up — absence-of-data rendered as data."""
        if not self.presence_dir.is_dir():
            return []
        now = time.time()
        rows: list[dict] = []
        for pf in self.presence_dir.glob("*.json"):
            try:
                d = json.loads(pf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            ts = _coerce_ts(d.get("ts"), _row_id=pf.stem)
            rows.append({"identity": d.get("identity") or pf.stem, "ts": ts, "age": now - ts})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows

    # ----- lease reads (transport-agnostic) ------------------------------- #
    # Coordination layer: one JSON file per (work_id, identity) under
    # <room>/leases/<work-id>__<identity>.json, OVERWRITTEN on (re)claim — so an
    # identity only ever writes its own file (conflict-free, like presence).
    # Contention on a work_id is resolved at READ time: among un-expired claims
    # the holder is the one with the EARLIEST claimed_at (first claimer wins;
    # later claimers see it's taken and back off). Kept out of chat.jsonl.

    @property
    def lease_dir(self) -> Path:
        return self.chat_file.parent / "leases"

    def _lease_file(self, work_id: str) -> Path:
        wid = re.sub(r"[^A-Za-z0-9._-]", "_", work_id)
        who = re.sub(r"[^A-Za-z0-9._-]", "_", self.identity)
        return self.lease_dir / f"{wid}__{who}.json"

    def _collect_leases(self) -> list[dict]:
        """One row per work_id: {work_id, holder, claimed_at, age, ttl, alive,
        contenders}. holder/alive reflect the earliest un-expired claim; alive is
        False when every claim for that work_id has expired (ttl seconds since its
        last heartbeat). Pure file read — no sync.

        SIBLING SWEEP of the same _coerce_ts class as _collect_presence above
        (review 2026-09-04): ts / claimed_at / ttl were parsed with bare `float()`
        outside the try, so one well-formed lease file with a string ts took the
        whole lease read down. Each field gets the direction that fails SAFE:
          ts         -> 0.0, so the claim reads EXPIRED (work free), never held.
          claimed_at -> falls back to ts. Coercing it independently to 0.0 would
                        make a corrupt claim win every race, since the holder is
                        min(claimed_at).
          ttl        -> None via _coerce_ttl, treated as expired below. 0.0 would
                        have meant "never expires" (see the alive test) — an
                        immortal lease on a corrupt file."""
        if not self.lease_dir.is_dir():
            return []
        now = time.time()
        by_work: dict[str, list[dict]] = {}
        for lf in self.lease_dir.glob("*.json"):
            try:
                d = json.loads(lf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            wid = d.get("work_id")
            if not wid:
                continue
            ts = _coerce_ts(d.get("ts"), _row_id=lf.stem)
            claimed_raw = d.get("claimed_at")
            claimed_at = _coerce_ts(claimed_raw, _row_id=lf.stem) if claimed_raw else ts
            by_work.setdefault(wid, []).append({
                "holder": d.get("holder") or lf.stem,
                "claimed_at": claimed_at or ts,
                "ts": ts,
                "ttl": _coerce_ttl(d.get("ttl")),
            })
        rows: list[dict] = []
        for wid, claims in by_work.items():
            # `ttl is None` = unreadable = NOT alive (see _coerce_ttl). Only a
            # ttl that genuinely parsed may claim the no-expiry (<= 0) branch.
            alive = [c for c in claims
                     if c["ttl"] is not None
                     and (c["ttl"] <= 0 or (now - c["ts"]) <= c["ttl"])]
            winner = min(alive, key=lambda c: c["claimed_at"]) if alive else None
            rows.append({
                "work_id": wid,
                "holder": winner["holder"] if winner else None,
                "claimed_at": winner["claimed_at"] if winner else None,
                "age": (now - winner["ts"]) if winner else None,
                "ttl": winner["ttl"] if winner else None,
                "alive": winner is not None,
                "contenders": sorted({c["holder"] for c in claims}),
            })
        rows.sort(key=lambda r: r["work_id"])
        return rows

    def _resolve_holder(self, work_id: str) -> dict | None:
        """The current un-expired holder row for work_id, or None if free."""
        for r in self._collect_leases():
            if r["work_id"] == work_id and r["alive"]:
                return r
        return None

    # ----- subclass surface ----------------------------------------------- #

    def commit_author_map(self) -> dict[str, str]:
        """Map message id8 → committer name, for `recv --verify-from`. Only the
        git transport can attribute a line to a committer; other transports have
        no commits, so they return {} → every message is 'unverifiable', and
        verify-from keeps them all (never treats absence as spoofing)."""
        return {}


class GitBusTransport(LocalJsonlBus):
    """Append-only JSONL in a git repo. Push on send, pull on recv.

    Constraints:
    - Designed for low-frequency exchange (agent-to-agent relay, not chatty).
    - Cross-machine concurrency is handled by rebase-on-push retry; same-machine
      concurrency is serialized by a best-effort send lock.
    - The bus repo MUST be a dedicated repo (never reuse a code repo); the
      chat log file lives at <bus>/<room>/chat.jsonl.
    """

    def __init__(self, bus_repo: Path, room: str, identity: str):
        super().__init__(bus_repo, room, identity)
        self.bus_repo = self.root
        if not (self.bus_repo / ".git").exists():
            raise RuntimeError(f"Not a git repo: {self.bus_repo}")
        if not (self.bus_repo / BUS_MARKER).exists():
            print(
                f"securedchat: warning — {self.bus_repo} has no {BUS_MARKER} marker; "
                f"a dedicated bus repo is expected (never point --bus at a code repo). "
                f"Run `init` to create it.",
                file=sys.stderr,
            )
        self.chat_file.parent.mkdir(parents=True, exist_ok=True)

    def _git(self, *args: str, check: bool = True,
             timeout: float | None = None) -> subprocess.CompletedProcess:
        """Every git call is bounded (review 2026-09-02: none was). A hung push held
        .send.lock forever; siblings broke the lock at 10s and ran git concurrently.
        GIT_TERMINAL_PROMPT=0 so a credential prompt fails instead of waiting on a TTY
        nobody is watching. A timeout is returned as rc=124 with a stderr that NAMES it
        (check=False) or raised as RuntimeError (check=True) -- never swallowed."""
        env = dict(os.environ)
        env.setdefault("GIT_TERMINAL_PROMPT", "0")
        t = GIT_TIMEOUT if timeout is None else timeout
        try:
            return subprocess.run(
                ["git", *args],
                cwd=self.bus_repo,
                check=check,
                capture_output=True,
                text=True,
                timeout=t,
                env=env,
            )
        except subprocess.TimeoutExpired as e:
            msg = f"git {' '.join(args[:2])} timed out after {t:g}s (GIT_TIMEOUT)"
            if check:
                raise RuntimeError(msg) from e
            return subprocess.CompletedProcess(e.cmd, 124, stdout=(e.stdout or b"").decode() if isinstance(e.stdout, bytes) else (e.stdout or ""), stderr=msg)

    def _has_remote(self) -> bool:
        return bool(self._git("remote", check=False).stdout.strip())

    def _ensure_gitattributes(self) -> bool:
        """Ensure the bus repo declares a union-merge driver for the append-only
        chat logs. Without it, two devices appending at EOF of chat.jsonl produce
        an add/add conflict that halts `pull --rebase`; `check=False` swallows the
        failure and the half-finished rebase wedges the repo for the next send.
        `merge=union` keeps both sides' appended lines — no conflict, no loss.
        Returns True if it wrote/updated the file (so the caller can stage it)."""
        ga = self.bus_repo / ".gitattributes"
        rules = ["chat.jsonl merge=union", "chat-*.jsonl merge=union"]
        existing = ga.read_text(encoding="utf-8") if ga.exists() else ""
        missing = [r for r in rules if r not in existing]
        if not missing:
            return False
        prefix = ""
        if not existing:
            prefix = ("# SecuredChat bus — chat logs are append-only JSONL; union-merge\n"
                      "# so concurrent appends from different devices never conflict.\n")
        elif not existing.endswith("\n"):
            prefix = "\n"
        with ga.open("a", encoding="utf-8") as f:
            f.write(prefix + "\n".join(missing) + "\n")
        return True

    def _pull_rebase(self) -> bool:
        """Sync with upstream (`fetch --no-write-fetch-head` + `rebase
        --autostash`), but DON'T discard the result. On failure, abort any
        half-finished rebase so the repo isn't left wedged for the next op,
        and warn loudly instead of silently serving stale local state as if
        current (the false "0 pending" failure). Returns True on success."""
        # Never route the sync through .git/FETCH_HEAD: it is a SHARED,
        # UNLOCKED file, so concurrent git activity in one clone (a second
        # session, a wake-monitor poll, a presence beat) appends entries and
        # `pull --rebase` dies with "Cannot rebase onto multiple branches" or
        # "no candidate for rebasing against" — FETCH_HEAD entry count ==
        # concurrency level, measured on 4 platform-contexts (2026-08-01).
        # NOTE: pinning remote+branch as `pull --rebase <remote> <branch>`
        # does NOT prevent this (this comment previously claimed it did;
        # measured false — the args pick the rebase target, they don't stop
        # other processes writing FETCH_HEAD). fetch --no-write-fetch-head +
        # explicit-ref rebase never touches the racy file: 0 fault-A failures
        # across all rigs. The concurrent ref-lock race on refs/remotes/* is
        # a SEPARATE fault this does not fix; it stays transient + warned.
        up = self._git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", check=False)
        if up.returncode == 0 and "/" in up.stdout.strip():
            remote, branch = up.stdout.strip().split("/", 1)
            res = self._git("fetch", "--no-write-fetch-head", "-q", remote, branch, check=False)
            if res.returncode == 0:
                res = self._git("rebase", "--autostash", f"{remote}/{branch}", check=False)
        else:
            # No resolvable upstream: fail LOUD rather than guess a branch.
            # The old bare `pull --rebase` fallback both raced on FETCH_HEAD
            # and guessed its target; a bus clone with a remote but no
            # upstream is a setup defect the operator should see, not one to
            # paper over (`git branch --set-upstream-to origin/main`).
            print(
                "securedchat: WARNING pull skipped (no upstream configured "
                "for this checkout — `git branch --set-upstream-to "
                "origin/main`?); local state may be stale.",
                file=sys.stderr,
            )
            self.last_pull_ok = False
            return False
        if res.returncode == 0:
            self.last_pull_ok = True
            return True
        git_dir = self.bus_repo / ".git"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            self._git("rebase", "--abort", check=False)  # unwedge for next op
        msg = _summarize_git_failure(res.stderr, res.stdout)
        print(
            f"securedchat: WARNING pull --rebase failed ({msg}); "
            "local state may be stale (offline or merge conflict).",
            file=sys.stderr,
        )
        self.last_pull_ok = False
        return False

    def _push_with_retry(self, what: str, attempts: int = PUSH_ATTEMPTS,
                         sleep=time.sleep) -> None:
        """Push, re-syncing between attempts. Raises RuntimeError on exhaustion.

        Replaces five hand-copied `for _ in range(3): push; pull` loops (send,
        archive, presence, lease acquire, lease release) that shared three
        defects. Two are worth stating because the obvious reading of that loop
        says it was fine:

        WHY BACKOFF, not just more attempts. Peers beat presence in BURSTS --
        measured on the live bus 2026-08-31, one box pushed 6 rooms in ~10s and
        the room saw 123 presence commits from 3 identities in 40 minutes. Three
        IMMEDIATE retries all land inside the same burst and lose to it, so the
        loop's failure was not "the remote is unreachable", it was "we kept
        arriving at the busiest moment". Sleeping first, THEN re-syncing, is what
        makes the next attempt land after the burst rather than inside it; the
        jitter is what stops two contending clones from re-colliding in lockstep.

        WHY THE PULL'S RESULT IS CARRIED. `_pull_rebase()` returns False for a
        genuinely different fault (offline, conflict, no upstream), and the old
        loops discarded it -- so a STALE-STATE failure and a LOST RACE both
        surfaced as the same "push rejected (fetch first)" text. They need
        opposite responses (fix the checkout vs. wait and retry), and the one
        fact that separates them was thrown away at the moment it existed. The
        message now names the cause when there is one.

        The third defect was minor: the old loops pulled after the final attempt
        and then raised, spending a network round-trip on state nothing reads."""
        result = None
        pull_failed = False
        prev = 0.0
        for i in range(attempts):
            result = self._git("push", check=False)
            if result.returncode == 0:
                return
            if i == attempts - 1:
                break  # no pull after the last attempt -- nothing would read it
            # Jittered doubling, CLAMPED to be strictly increasing. With a jitter band
            # whose ratio exceeds 2 (0.4..1.2), attempt i+1 could draw shorter than
            # attempt i (measured 2026-09-02: 0.92s then 0.81s), which made the
            # "strictly increasing" corpus arm flaky -- a gate that fails at random
            # gets switched off. Growth is now guaranteed; the jitter stays.
            delay = random.uniform(PUSH_BACKOFF_MIN, PUSH_BACKOFF_MAX) * (2 ** i)
            delay = max(delay, prev * 1.5) if prev else delay
            prev = delay
            sleep(delay)
            if not self._pull_rebase():
                pull_failed = True
        detail = ((result.stderr if result else "") or "").strip()
        cause = (" -- and a re-sync pull ALSO failed, so this is STALE LOCAL "
                 "STATE, not a lost race" if pull_failed else "")
        raise RuntimeError(
            f"{what} push failed after {attempts} attempts{cause}: {detail}")

    def init(self) -> str:
        """Create the bus marker + room chat.jsonl (+ union-merge .gitattributes)
        and commit them. Idempotent. Returns a human-readable status line."""
        to_add: list[str] = []
        already = None
        marker = self.bus_repo / BUS_MARKER
        if not marker.exists():
            marker.write_text("securedchat bus repo — agent-to-agent chat only, never store code here\n")
            to_add.append(str(marker.relative_to(self.bus_repo)))
        if self._ensure_gitattributes():
            to_add.append(".gitattributes")
        if self.chat_file.exists():
            already = f"room already initialized: {self.chat_file}"
        else:
            self.chat_file.touch()
            to_add.append(str(self.chat_file.relative_to(self.bus_repo)))
        if not to_add:
            return already or f"room already initialized: {self.chat_file}"
        for rel in to_add:
            self._git("add", rel)
        self._git("commit", "-m", f"chat: init room {self.room}")
        tail = ("\n(remember to `git push` from the bus repo to publish)"
                if self._has_remote() else
                "\n(local bus, no remote — nothing to push)")
        return f"initialized: {', '.join(to_add)}{tail}"

    def send(self, msg: Message) -> None:
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            ga_added = self._ensure_gitattributes()
            with self.chat_file.open("a", encoding="utf-8") as f:
                f.write(msg.to_jsonl() + "\n")
            rel = self.chat_file.relative_to(self.bus_repo)
            add = self._git("add", str(rel), check=False)
            if add.returncode != 0:
                raise RuntimeError(f"git add failed: {add.stderr.strip()}")
            if ga_added:
                self._git("add", ".gitattributes", check=False)
            commit = self._git(
                "-c", f"user.email={self.identity}@securedchat-cli",
                "-c", f"user.name={self.identity}",
                "commit", "-m", f"chat: {self.room} {msg.id[:8]}",
                check=False,
            )
            if commit.returncode != 0:
                raise RuntimeError(
                    f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
                )
            if not self._has_remote():
                # P10: "sent" must not silently imply delivery. With no remote,
                # the message is committed LOCALLY ONLY and can reach no peer.
                # Warn loudly (like _pull_rebase) rather than let "sent" lie.
                print(
                    f"securedchat: WARNING message {msg.id[:8]} committed LOCALLY "
                    "ONLY — no remote configured, NOT published to any peer.",
                    file=sys.stderr,
                )
                return
            try:
                self._push_with_retry("message")
            except RuntimeError as e:
                # The row IS committed locally and WILL publish on this clone's next
                # successful push -- any push, including a presence beat. A resend
                # after this message would put the same text on the bus twice.
                raise RuntimeError(
                    f"{e}\nsecuredchat: message {msg.id[:8]} is committed LOCALLY and will "
                    f"publish on the next successful push from this clone -- do NOT resend it."
                ) from e

    def recv(self, since_id: str | None = None) -> list[Message]:
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            return self._recv_resolved(since_id)

    def compact(self, keep_last: int = 200) -> int:
        """Move all-but-last-`keep_last` active messages into a new archive
        segment and rewrite chat.jsonl with only the recent tail.

        History is preserved — archive segments are stitched back by _read_all,
        so `recv --id <old>` and an archived cursor still resolve. This REWRITES
        chat.jsonl, so run it when the channel is quiet: a concurrent send on
        another machine forces a rebase of the rewrite. Returns the number of
        messages archived (0 = nothing to do).
        """
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            active = self._read_file(self.chat_file)
            if len(active) <= keep_last:
                return 0
            to_archive = active if keep_last == 0 else active[:-keep_last]
            keep = [] if keep_last == 0 else active[-keep_last:]

            self.archive_dir.mkdir(parents=True, exist_ok=True)
            # Nanosecond, zero-padded name → lexicographic == chronological even
            # for two compactions within the SAME second. Whole-second names
            # collided, leaving the random uuid suffix to decide segment order —
            # an intermittent stitch-back mis-ordering bug. Later segments
            # (newer messages) now reliably sort after earlier ones.
            seg_path = self.archive_dir / f"chat-{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.jsonl"
            with seg_path.open("w", encoding="utf-8") as f:
                for m in to_archive:
                    f.write(m.to_jsonl() + "\n")
            self._write_archive_index(seg_path, to_archive)
            tmp = self.chat_file.with_name(self.chat_file.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for m in keep:
                    f.write(m.to_jsonl() + "\n")
            os.replace(tmp, self.chat_file)

            for rel in (seg_path, self.chat_file, self._archive_index):
                add = self._git("add", str(rel.relative_to(self.bus_repo)), check=False)
                if add.returncode != 0:
                    raise RuntimeError(f"git add failed: {add.stderr.strip()}")
            commit = self._git(
                "-c", f"user.email={self.identity}@securedchat-cli",
                "-c", f"user.name={self.identity}",
                "commit", "-m", f"chat: compact {self.room} ({len(to_archive)} archived)",
                check=False,
            )
            if commit.returncode != 0:
                raise RuntimeError(
                    f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
                )
            if self._has_remote():
                self._push_with_retry("archive")
        return len(to_archive)

    def commit_author_map(self) -> dict[str, str]:
        """Map message id8 → git commit author name, parsed from commit subjects
        of the form 'chat: <room> <id8>' (the format `send` writes). Lets `recv`
        cross-check a message's claimed `from` against who actually committed it.

        Messages not committed via the CLI, or whose subject doesn't match, simply
        don't appear → callers treat them as UNVERIFIABLE, never as spoofed.

        IMPORTANT — this is NOT authentication. `send` sets the git author from the
        --identity flag (`-c user.name=identity`), so the author reflects who
        *committed* the line, not a verified identity; anyone with write access to
        the bus repo can set any author. This catches accidental/sloppy mislabeling,
        not a determined forger. Real per-sender auth needs signed bodies (roadmap).
        """
        sep = "\x1f"
        out = self._git("log", f"--format=%an{sep}%s", check=False)
        if out.returncode != 0:
            return {}
        pat = re.compile(rf"^chat: {re.escape(self.room)} ([0-9a-f]{{8}})$")
        amap: dict[str, str] = {}
        for line in out.stdout.splitlines():
            author, _, subject = line.partition(sep)
            m = pat.match(subject)
            if m:
                amap.setdefault(m.group(1), author)  # first (newest) wins
        return amap

    # ----- presence / liveness -------------------------------------------- #
    # One small JSON file per identity under <room>/presence/, OVERWRITTEN each
    # heartbeat (never appended) → the working tree never grows, and different
    # identities never conflict (each writes only its own file). Kept out of
    # chat.jsonl so presence chatter doesn't pollute the message log.

    def announce_presence(self, meta: dict | None = None) -> None:
        """Overwrite this identity's presence file with a fresh timestamp, then
        commit + push. Conflict-free by construction (one writer per file)."""
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            self.presence_dir.mkdir(parents=True, exist_ok=True)
            pfile = self.presence_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '_', self.identity)}.json"
            payload = {"identity": self.identity, "ts": time.time(), "kind": "presence"}
            if meta:
                payload.update(meta)
            pfile.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            add = self._git("add", str(pfile.relative_to(self.bus_repo)), check=False)
            if add.returncode != 0:
                raise RuntimeError(f"git add failed: {add.stderr.strip()}")
            commit = self._git(
                "-c", f"user.email={self.identity}@securedchat-cli",
                "-c", f"user.name={self.identity}",
                "commit", "-m", f"presence: {self.room} {self.identity}",
                check=False,
            )
            if commit.returncode != 0:
                blob = (commit.stdout + commit.stderr).lower()
                if "nothing to commit" in blob:
                    return  # unchanged within the same instant — benign
                raise RuntimeError(
                    f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}"
                )
            if self._has_remote():
                self._push_with_retry("presence")

    def read_presence(self, pull: bool = True) -> list[dict]:
        """Return [{identity, ts, age}] for every presence file, newest first.
        Pass pull=False to skip the git pull when the caller already refreshed
        (e.g. the dashboard right after recv)."""
        if pull and self._has_remote():
            with self._send_lock():
                self._pull_rebase()
        return self._collect_presence()

    # ----- task leases (claim / release / read) --------------------------- #
    # Same conflict-free one-file-per-(work,identity) model as presence, plus
    # commit/push so other devices see the claim. Acquire refuses if a *different*
    # identity already holds an un-expired lease on the work_id; re-acquiring as
    # the holder renews it (preserving the original claimed_at).

    def acquire_lease(self, work_id: str, ttl: float = 1800.0) -> dict:
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            held = self._resolve_holder(work_id)
            if held and held["holder"] != self.identity:
                return {"status": "conflict", "work_id": work_id, "holder": held["holder"],
                        "age": held["age"], "ttl": held["ttl"]}
            self.lease_dir.mkdir(parents=True, exist_ok=True)
            lf = self._lease_file(work_id)
            now = time.time()
            claimed_at, renew = now, False
            if lf.exists():
                # Same class as _collect_leases (review 2026-09-04): the try below
                # catches JSONDecodeError/OSError but NOT the ValueError a bare
                # float() raises on a well-formed file with a string claimed_at —
                # which made acquiring a lease crash on a corrupt prior claim of
                # our OWN. Coerced instead, and an unreadable claimed_at falls back
                # to `now`: never to 0.0, which would backdate our claim to the
                # epoch and win every race by min(claimed_at).
                try:
                    prior = json.loads(lf.read_text(encoding="utf-8"))
                    claimed_at = _coerce_ts(prior.get("claimed_at"), _row_id=lf.stem) or now
                    renew = True
                except (json.JSONDecodeError, OSError):
                    pass
            payload = {"work_id": work_id, "holder": self.identity, "claimed_at": claimed_at,
                       "ts": now, "ttl": ttl, "kind": "lease"}
            lf.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            add = self._git("add", str(lf.relative_to(self.bus_repo)), check=False)
            if add.returncode != 0:
                raise RuntimeError(f"git add failed: {add.stderr.strip()}")
            commit = self._git(
                "-c", f"user.email={self.identity}@securedchat-cli",
                "-c", f"user.name={self.identity}",
                "commit", "-m", f"lease: {self.room} {work_id} {self.identity}", check=False,
            )
            if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
                raise RuntimeError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
            if self._has_remote():
                self._push_with_retry("lease")
                self._pull_rebase()  # re-resolve who actually won a near-simultaneous race
            winner = self._resolve_holder(work_id)
            if winner and winner["holder"] != self.identity:
                return {"status": "conflict", "work_id": work_id, "holder": winner["holder"],
                        "age": winner["age"], "ttl": winner["ttl"]}
            return {"status": "renewed" if renew else "acquired", "work_id": work_id,
                    "holder": self.identity, "ttl": ttl}

    def release_lease(self, work_id: str) -> dict:
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            lf = self._lease_file(work_id)
            if not lf.exists():
                return {"status": "not-held", "work_id": work_id}
            rel = str(lf.relative_to(self.bus_repo))
            lf.unlink()
            self._git("add", rel, check=False)  # stages the deletion
            commit = self._git(
                "-c", f"user.email={self.identity}@securedchat-cli",
                "-c", f"user.name={self.identity}",
                "commit", "-m", f"lease-release: {self.room} {work_id} {self.identity}", check=False,
            )
            if commit.returncode != 0 and "nothing to commit" not in (commit.stdout + commit.stderr).lower():
                raise RuntimeError(f"git commit failed: {commit.stderr.strip() or commit.stdout.strip()}")
            if self._has_remote():
                self._push_with_retry("lease release")
            return {"status": "released", "work_id": work_id}

    def read_leases(self, pull: bool = True) -> list[dict]:
        """All leases (one row per work_id), earliest-claimer resolved. Pass
        pull=False to skip the git pull when the caller already refreshed."""
        if pull and self._has_remote():
            with self._send_lock():
                self._pull_rebase()
        return self._collect_leases()


class FileBusTransport(LocalJsonlBus):
    """Append-only JSONL in a plain directory — no git, no server.

    The bus is just a folder: same machine (multiple agents, zero network), or a
    shared/synced one (NAS mount, Syncthing/Dropbox) for same-LAN reach. `send`
    appends; `recv` reads; a best-effort lock serializes same-host writers.

    Trade-offs vs the git transport:
    - No cross-host conflict resolution beyond what the filesystem itself does.
      A true shared POSIX filesystem makes small O_APPEND writes atomic; folder-
      sync tools (Dropbox/Syncthing) can produce their own *.sync-conflict copies
      if two hosts append at the exact same moment. Fine for low cadence.
    - No commit author → `recv --verify-from` has nothing to check (every message
      is 'unverifiable' and kept). Trust boundary is who can write the folder.
    - compact()/presence work identically, just without a commit/push.
    """

    def __init__(self, bus_dir: Path, room: str, identity: str):
        super().__init__(bus_dir, room, identity)
        self.bus_dir = self.root
        if self.bus_dir.exists() and not (self.bus_dir / BUS_MARKER).exists():
            print(
                f"securedchat: warning — {self.bus_dir} has no {BUS_MARKER} marker; "
                f"a dedicated bus directory is expected (never point --bus at a code "
                f"tree). Run `init` to create it.",
                file=sys.stderr,
            )
        self.chat_file.parent.mkdir(parents=True, exist_ok=True)

    def init(self) -> str:
        """Create the bus marker + room chat.jsonl in the directory. Idempotent."""
        created: list[str] = []
        self.bus_dir.mkdir(parents=True, exist_ok=True)
        marker = self.bus_dir / BUS_MARKER
        if not marker.exists():
            marker.write_text("securedchat bus dir — agent-to-agent chat only, never store code here\n")
            created.append(str(marker))
        if self.chat_file.exists():
            return f"room already initialized: {self.chat_file}"
        self.chat_file.touch()
        created.append(str(self.chat_file))
        return f"initialized (file transport, no git): {', '.join(created)}"

    def send(self, msg: Message) -> None:
        with self._send_lock():
            with self.chat_file.open("a", encoding="utf-8") as f:
                f.write(msg.to_jsonl() + "\n")

    def recv(self, since_id: str | None = None) -> list[Message]:
        with self._send_lock():
            return self._recv_resolved(since_id)

    def compact(self, keep_last: int = 200) -> int:
        """Archive all-but-last-`keep_last` messages and rewrite chat.jsonl with
        the recent tail. Same stitching contract as the git transport, no commit.
        Returns the number archived (0 = nothing to do)."""
        if keep_last < 0:
            raise ValueError("keep_last must be >= 0")
        with self._send_lock():
            active = self._read_file(self.chat_file)
            if len(active) <= keep_last:
                return 0
            to_archive = active if keep_last == 0 else active[:-keep_last]
            keep = [] if keep_last == 0 else active[-keep_last:]
            self.archive_dir.mkdir(parents=True, exist_ok=True)
            seg_path = self.archive_dir / f"chat-{time.time_ns():020d}-{uuid.uuid4().hex[:8]}.jsonl"
            with seg_path.open("w", encoding="utf-8") as f:
                for m in to_archive:
                    f.write(m.to_jsonl() + "\n")
            self._write_archive_index(seg_path, to_archive)
            tmp = self.chat_file.with_name(self.chat_file.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for m in keep:
                    f.write(m.to_jsonl() + "\n")
            os.replace(tmp, self.chat_file)
        return len(to_archive)

    def announce_presence(self, meta: dict | None = None) -> None:
        """Overwrite this identity's presence file with a fresh timestamp.
        One writer per file → conflict-free, no commit."""
        with self._send_lock():
            self.presence_dir.mkdir(parents=True, exist_ok=True)
            pfile = self.presence_dir / f"{re.sub(r'[^A-Za-z0-9._-]', '_', self.identity)}.json"
            payload = {"identity": self.identity, "ts": time.time(), "kind": "presence"}
            if meta:
                payload.update(meta)
            pfile.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")

    def read_presence(self, pull: bool = True) -> list[dict]:
        return self._collect_presence()

    # ----- task leases (claim / release / read) — no git --------------------- #

    def acquire_lease(self, work_id: str, ttl: float = 1800.0) -> dict:
        with self._send_lock():
            held = self._resolve_holder(work_id)
            if held and held["holder"] != self.identity:
                return {"status": "conflict", "work_id": work_id, "holder": held["holder"],
                        "age": held["age"], "ttl": held["ttl"]}
            self.lease_dir.mkdir(parents=True, exist_ok=True)
            lf = self._lease_file(work_id)
            now = time.time()
            claimed_at, renew = now, False
            if lf.exists():
                # Same class as _collect_leases (review 2026-09-04): the try below
                # catches JSONDecodeError/OSError but NOT the ValueError a bare
                # float() raises on a well-formed file with a string claimed_at —
                # which made acquiring a lease crash on a corrupt prior claim of
                # our OWN. Coerced instead, and an unreadable claimed_at falls back
                # to `now`: never to 0.0, which would backdate our claim to the
                # epoch and win every race by min(claimed_at).
                try:
                    prior = json.loads(lf.read_text(encoding="utf-8"))
                    claimed_at = _coerce_ts(prior.get("claimed_at"), _row_id=lf.stem) or now
                    renew = True
                except (json.JSONDecodeError, OSError):
                    pass
            payload = {"work_id": work_id, "holder": self.identity, "claimed_at": claimed_at,
                       "ts": now, "ttl": ttl, "kind": "lease"}
            lf.write_text(json.dumps(payload, ensure_ascii=False) + "\n", encoding="utf-8")
            return {"status": "renewed" if renew else "acquired", "work_id": work_id,
                    "holder": self.identity, "ttl": ttl}

    def release_lease(self, work_id: str) -> dict:
        with self._send_lock():
            lf = self._lease_file(work_id)
            if not lf.exists():
                return {"status": "not-held", "work_id": work_id}
            lf.unlink()
            return {"status": "released", "work_id": work_id}

    def read_leases(self, pull: bool = True) -> list[dict]:
        return self._collect_leases()


class _BackgroundLoop:
    """A private asyncio event loop on a daemon thread, so synchronous callers
    (the CLI) can drive aiortc's async API. `run` submits a coroutine and blocks
    for its result."""

    def __init__(self):
        import asyncio
        import threading
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def _run(self) -> None:
        import asyncio
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    def run(self, coro, timeout: float | None = None):
        import asyncio
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def stop(self) -> None:
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        if not self.loop.is_closed():
            self.loop.close()


class WebRTCTransport(LocalJsonlBus):
    """EXPERIMENTAL real-time peer-to-peer transport (aiortc data channel).

    The SDP offer/answer handshake is bootstrapped over a *signaling* bus (a
    GitBusTransport or FileBusTransport): one peer writes a `kind:sdp-offer`
    message addressed to the other, who replies `kind:sdp-answer`. Once the data
    channel opens, live messages flow directly peer-to-peer (DTLS-encrypted) and
    never touch the signaling bus again — git/the bus is used ONLY for the
    one-time handshake.

    Durability: this transport keeps its OWN local append-only log (under
    ~/.config/securedchat/webrtc/<key>/<room>/chat.jsonl) of messages it sent and
    received over the channel, so recv / cursor / watch work locally. It does NOT
    persist live traffic to the signaling bus — WebRTC is online-only (both peers
    must be connected at once). If you need durable, offline, store-and-forward
    delivery, use --transport git.

    Requires `pip install aiortc` (optional dependency; the git/file transports
    need nothing beyond the stdlib). Cross-NAT / cross-machine connectivity
    depends on STUN and the network and is NOT exercised by the automated tests —
    only a same-process loopback path is. Treat as experimental.

    TRUST: the SDP offer/answer rides the (unauthenticated) signaling bus, so a
    WebRTC session's trust == bus-write trust. Anyone who can write the signaling
    bus could post a rogue `sdp-answer` as the peer and MITM the session (the
    offerer takes the first matching answer). Keep the signaling bus private and
    its writers trusted. A pre-shared SDP-fingerprint check is roadmap.
    """

    SIGNAL_OFFER = "sdp-offer"
    SIGNAL_ANSWER = "sdp-answer"
    DEFAULT_ICE = ["stun:stun.l.google.com:19302"]

    def __init__(self, signaling: "Transport", room: str, identity: str,
                 state_dir: Path | None = None, ice_servers: list[str] | None = None):
        import hashlib
        sig_key = hashlib.sha1(
            str(getattr(signaling, "root", "mem")).encode("utf-8")
        ).hexdigest()[:12]
        base = Path(state_dir) if state_dir else (
            Path.home() / ".config" / "securedchat" / "webrtc" / sig_key)
        super().__init__(base, room, identity)
        self.chat_file.parent.mkdir(parents=True, exist_ok=True)
        self.signaling = signaling
        self.ice_servers = self.DEFAULT_ICE if ice_servers is None else list(ice_servers)
        self._loop: _BackgroundLoop | None = None
        self._pc = None        # RTCPeerConnection
        self._channel = None   # RTCDataChannel
        self._sig_cursor: str | None = None  # signaling-bus read cursor

    # ----- async plumbing ------------------------------------------------- #

    def _ensure_loop(self) -> _BackgroundLoop:
        if self._loop is None:
            self._loop = _BackgroundLoop()
        return self._loop

    def _append_local(self, msg: Message) -> None:
        with self._send_lock():
            with self.chat_file.open("a", encoding="utf-8") as f:
                f.write(msg.to_jsonl() + "\n")

    def _sig_send(self, peer: str, kind: str, desc) -> None:
        body = json.dumps({"sdp": desc.sdp, "type": desc.type})
        self.signaling.send(Message.new(from_=self.identity, to=peer, body=body, kind=kind))

    # ----- connection lifecycle ------------------------------------------- #

    def connect(self, peer: str, role: str, timeout: float = 60.0) -> None:
        """Establish a data channel to `peer`.

        role='offer'  → create the offer, publish it, wait for the answer.
        role='answer' → wait for the offer, reply with the answer.
        Blocks until the channel is open or `timeout` elapses. The two peers must
        agree on roles out of band (e.g. the offerer starts first)."""
        if role not in ("offer", "answer"):
            raise ValueError("role must be 'offer' or 'answer'")
        self._ensure_loop().run(self._connect(peer, role, timeout), timeout=timeout + 15)

    async def _connect(self, peer: str, role: str, timeout: float) -> None:
        try:
            from aiortc import (RTCConfiguration, RTCIceServer, RTCPeerConnection,
                                RTCSessionDescription)
        except ImportError as e:
            raise RuntimeError(
                "WebRTC transport needs aiortc — install it with `pip install aiortc` "
                "(optional; the git/file transports need nothing extra)."
            ) from e
        import asyncio

        config = RTCConfiguration([RTCIceServer(urls=u) for u in self.ice_servers])
        pc = RTCPeerConnection(config)
        self._pc = pc
        opened: "asyncio.Future[bool]" = asyncio.get_running_loop().create_future()

        def _wire(ch) -> None:
            self._channel = ch

            @ch.on("open")
            def _on_open() -> None:
                if not opened.done():
                    opened.set_result(True)

            @ch.on("message")
            def _on_message(data) -> None:
                if isinstance(data, (bytes, bytearray)):
                    data = data.decode("utf-8", "replace")
                try:
                    m = Message.from_jsonl(data)
                except Exception:
                    return
                if m.id:
                    self._append_local(m)

            # The answerer receives the channel via the `datachannel` event, by
            # which point it may ALREADY be open — the "open" event then fires
            # before this handler is attached and would be missed. Resolve now if
            # so; otherwise the on("open") handler above catches the transition.
            if getattr(ch, "readyState", None) == "open" and not opened.done():
                opened.set_result(True)

        if role == "offer":
            _wire(pc.createDataChannel("chat"))
            await pc.setLocalDescription(await pc.createOffer())
            await self._wait_ice(pc, timeout)
            self._sig_send(peer, self.SIGNAL_OFFER, pc.localDescription)
            ans = await self._await_signal(self.SIGNAL_ANSWER, peer, timeout)
            await pc.setRemoteDescription(RTCSessionDescription(ans["sdp"], ans["type"]))
        else:
            @pc.on("datachannel")
            def _on_dc(ch) -> None:
                _wire(ch)

            offer = await self._await_signal(self.SIGNAL_OFFER, peer, timeout)
            await pc.setRemoteDescription(RTCSessionDescription(offer["sdp"], offer["type"]))
            await pc.setLocalDescription(await pc.createAnswer())
            await self._wait_ice(pc, timeout)
            self._sig_send(peer, self.SIGNAL_ANSWER, pc.localDescription)

        await asyncio.wait_for(opened, timeout=timeout)

    async def _wait_ice(self, pc, timeout: float = 30.0) -> None:
        """Wait for ICE gathering to finish so localDescription carries all
        candidates (non-trickle): we ship one complete SDP over the bus rather
        than streaming candidates. Bounded by `timeout` so a network that never
        finishes gathering can't hang the handshake forever."""
        import asyncio
        if pc.iceGatheringState == "complete":
            return
        done: "asyncio.Future[None]" = asyncio.get_running_loop().create_future()

        @pc.on("icegatheringstatechange")
        def _on_change() -> None:
            if pc.iceGatheringState == "complete" and not done.done():
                done.set_result(None)

        await asyncio.wait_for(done, timeout=timeout)

    async def _await_signal(self, kind: str, peer: str, timeout: float) -> dict:
        """Poll the signaling bus for a well-formed SDP frame of `kind` from
        `peer`. Malformed/hostile frames are skipped, not fatal."""
        import asyncio
        deadline = time.time() + timeout
        loop = asyncio.get_running_loop()
        while time.time() < deadline:
            msgs = await loop.run_in_executor(
                None, lambda: self.signaling.recv(since_id=self._sig_cursor))
            for m in msgs:
                self._sig_cursor = m.id
                if not (m.kind == kind and m.from_ == peer and m.to in (None, self.identity)):
                    continue
                try:
                    desc = json.loads(m.body)
                except (json.JSONDecodeError, ValueError):
                    continue  # not JSON — skip, keep waiting
                if isinstance(desc, dict) and "sdp" in desc and "type" in desc:
                    return desc
                # well-addressed but malformed SDP frame — ignore, keep waiting
            await asyncio.sleep(1.0)
        raise TimeoutError(f"timed out after {timeout:g}s waiting for {kind} from {peer!r}")

    # ----- Transport surface ---------------------------------------------- #

    def send(self, msg: Message) -> None:
        """Deliver over the data channel if connected; always append to the local
        log so the sender's own recv/history include it. With no open channel the
        message is local-only (WebRTC is online-only — start a session first)."""
        self._append_local(msg)
        ch = self._channel
        if ch is not None and getattr(ch, "readyState", None) == "open":
            self._ensure_loop().run(self._ch_send(ch, msg.to_jsonl()), timeout=10)
        else:
            print(
                "securedchat: WebRTC has no open peer channel — message stored "
                "locally only (establish a session with `connect`).",
                file=sys.stderr,
            )

    async def _ch_send(self, ch, data: str) -> None:
        ch.send(data)

    def recv(self, since_id: str | None = None) -> list[Message]:
        with self._send_lock():
            return self._recv_resolved(since_id)

    def close(self) -> None:
        if self._pc is not None and self._loop is not None:
            try:
                self._loop.run(self._pc.close(), timeout=10)
            except Exception:
                pass
        if self._loop is not None:
            self._loop.stop()
            self._loop = None
