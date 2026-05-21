"""Transport layer for SecuredChat CLI.

Defines a Transport abstraction so the CLI can swap out delivery mechanisms
without changing the user-facing commands. Today: GitBusTransport (append-
only JSONL in a private git repo, push/pull as the sync primitive). Tomorrow:
WebRTCTransport (aiortc data channel, with SDP handshake bootstrapped over
the git bus).

Message wire format (JSONL line):

    {"ts": <unix-float>, "id": <uuid>, "from": <str>, "to": <str-or-null>,
     "kind": "msg", "body": <str>, "reply_to": <id-or-absent>}

`to: null` = broadcast within the room. `kind` reserved for future control
frames (sdp-offer, sdp-answer, presence, etc.). `reply_to` is the id of the
message this one answers (threading); absent when not a reply.

Parsing is deliberately tolerant: unknown keys are ignored (a newer peer may
add fields) and missing keys are defaulted (an older peer may omit them), so a
schema change on one side never makes the other silently drop messages.
"""
from __future__ import annotations

import json
import os
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

# Marker file that identifies a repo as a dedicated SecuredChat bus (never a
# code repo). `init` creates it; the transport warns if it is missing.
BUS_MARKER = ".securedchat-bus"

# Identity/room must be safe for git author/subject AND filenames — reject
# anything that could inject into `git -c user.name=...`, a commit subject, or a
# path (newline, '=', '/', '..', control chars).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")


@dataclass
class Message:
    ts: float
    id: str
    from_: str
    to: str | None
    kind: str
    body: str
    reply_to: str | None = None

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
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> "Message":
        # Tolerant by design: ignore unknown keys, default missing ones.
        # Only a JSON syntax error makes a line unparseable (handled by recv).
        d = json.loads(line)
        from_ = d.get("from", d.get("from_", ""))
        return cls(
            ts=float(d.get("ts") or 0.0),
            id=str(d.get("id", "")),
            from_=str(from_),
            to=d.get("to"),
            kind=str(d.get("kind", "msg")),
            body=str(d.get("body", "")),
            reply_to=d.get("reply_to"),
        )


class Transport(ABC):
    @abstractmethod
    def send(self, msg: Message) -> None: ...

    @abstractmethod
    def recv(self, since_id: str | None = None) -> list[Message]: ...

    @abstractmethod
    def watch(self, poll_seconds: float = 5.0, since_id: str | None = None) -> Iterator[Message]: ...


class GitBusTransport(Transport):
    """Append-only JSONL in a git repo. Push on send, pull on recv.

    Constraints:
    - Designed for low-frequency exchange (agent-to-agent relay, not chatty).
    - Cross-machine concurrency is handled by rebase-on-push retry; same-machine
      concurrency is serialized by a best-effort send lock.
    - The bus repo MUST be a dedicated repo (never reuse a code repo); the
      chat log file lives at <bus>/<room>/chat.jsonl.
    """

    def __init__(self, bus_repo: Path, room: str, identity: str):
        if not _SAFE_NAME.match(room or ""):
            raise RuntimeError(f"invalid room {room!r}: use only letters, digits, . _ -")
        if not _SAFE_NAME.match(identity or ""):
            raise RuntimeError(f"invalid identity {identity!r}: use only letters, digits, . _ -")
        self.bus_repo = Path(bus_repo).resolve()
        self.room = room
        self.identity = identity
        self.last_pull_ok = True  # set False by _pull_rebase on a failed pull (R2)
        self.chat_file = self.bus_repo / room / "chat.jsonl"
        # Compaction moves old messages here as chat-<seq>.jsonl segments; the
        # active chat.jsonl keeps only the recent tail. _read_all stitches
        # archive segments + active back together so history is never lost.
        self.archive_dir = self.chat_file.parent / "archive"
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

    def _git(self, *args: str, check: bool = True) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["git", *args],
            cwd=self.bus_repo,
            check=check,
            capture_output=True,
            text=True,
        )

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
        """`pull --rebase --autostash`, but DON'T discard the result. On failure,
        abort any half-finished rebase so the repo isn't left wedged for the next
        op, and warn loudly instead of silently serving stale local state as if
        current (the false "0 pending" failure). Returns True on success."""
        res = self._git("pull", "--rebase", "--autostash", check=False)
        if res.returncode == 0:
            self.last_pull_ok = True
            return True
        git_dir = self.bus_repo / ".git"
        if (git_dir / "rebase-merge").exists() or (git_dir / "rebase-apply").exists():
            self._git("rebase", "--abort", check=False)  # unwedge for next op
        msg = (res.stderr or res.stdout or "").strip().replace("\n", " ")
        print(
            f"securedchat: WARNING pull --rebase failed ({msg[:200]}); "
            "local state may be stale (offline or merge conflict).",
            file=sys.stderr,
        )
        self.last_pull_ok = False
        return False

    def _id_resolves(self, since_id: str) -> bool:
        """True iff `since_id` uniquely matches a message currently in the log
        (full history). Used by watch to tell a stale cursor (re-anchor) apart
        from a valid cursor with simply no new messages (keep waiting)."""
        matches = [m for m in self._read_all(include_archive=True) if m.id.startswith(since_id)]
        return len(matches) == 1

    @contextmanager
    def _send_lock(self, timeout: float = 10.0):
        """Best-effort advisory lock serializing repo-mutating git ops on ONE machine.

        Held by `send` (pull + append + commit + push) AND by `recv` (its
        `git pull --rebase --autostash` + file read). Without it, a same-machine
        recv could pull/rebase while a send is mid-commit, or read a half-written
        line. Cross-machine concurrency is still covered by push rebase-retry. If
        the lock can't be acquired within `timeout` (e.g. a crashed holder left a
        stale lock), the stale lock is broken and we proceed rather than block.
        """
        lock_path = self.chat_file.parent / ".send.lock"
        deadline = time.time() + timeout
        fd = None
        while True:
            try:
                fd = os.open(str(lock_path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                break
            except FileExistsError:
                try:
                    age = time.time() - lock_path.stat().st_mtime
                except FileNotFoundError:
                    continue
                if age > timeout or time.time() > deadline:
                    lock_path.unlink(missing_ok=True)  # break stale lock, then retry
                    continue
                time.sleep(0.1)
        try:
            yield
        finally:
            if fd is not None:
                os.close(fd)
                lock_path.unlink(missing_ok=True)

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
                return
            result = None
            for _ in range(3):
                result = self._git("push", check=False)
                if result.returncode == 0:
                    return
                self._pull_rebase()
            raise RuntimeError(f"push failed after retries: {result.stderr if result else ''}")

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

    def recv(self, since_id: str | None = None) -> list[Message]:
        with self._send_lock():
            if self._has_remote():
                self._pull_rebase()
            # Fast path: a full-length cursor (mark-seen writes the full id) that
            # lands in the active tail means everything after it is also in the
            # active tail — skip reading archive segments. Short `--since`
            # prefixes take the full-history path to preserve exact prefix /
            # ambiguity semantics.
            if since_id is not None and len(since_id) >= 32:
                active = self._read_all(include_archive=False)
                hits = [i for i, m in enumerate(active) if m.id.startswith(since_id)]
                if len(hits) == 1:
                    return active[hits[0] + 1:]
                # 0 hits → cursor is archived or stale; >1 → ambiguous in active.
                # Both fall through to full-history resolution below.
            msgs = self._read_all(include_archive=True)
        if since_id is None:
            return msgs
        # Match by id prefix (consistent with `recv --id`). A stale/ambiguous
        # cursor returns NOTHING rather than silently replaying the whole backlog.
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
        return msgs[matches[0] + 1:]

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
            tmp = self.chat_file.with_name(self.chat_file.name + ".tmp")
            with tmp.open("w", encoding="utf-8") as f:
                for m in keep:
                    f.write(m.to_jsonl() + "\n")
            os.replace(tmp, self.chat_file)

            for rel in (seg_path, self.chat_file):
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
                result = None
                for _ in range(3):
                    result = self._git("push", check=False)
                    if result.returncode == 0:
                        break
                    self._pull_rebase()
                else:
                    raise RuntimeError(f"push failed after retries: {result.stderr if result else ''}")
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

    @property
    def presence_dir(self) -> Path:
        return self.chat_file.parent / "presence"

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
                result = None
                for _ in range(3):
                    result = self._git("push", check=False)
                    if result.returncode == 0:
                        return
                    self._pull_rebase()
                raise RuntimeError(f"presence push failed after retries: {result.stderr if result else ''}")

    def read_presence(self, pull: bool = True) -> list[dict]:
        """Return [{identity, ts, age}] for every presence file, newest first.
        `age` is seconds since that identity's last heartbeat. Callers apply
        their own online/stale window. Pass pull=False to skip the git pull when
        the caller already refreshed (e.g. the dashboard right after recv)."""
        if pull and self._has_remote():
            with self._send_lock():
                self._pull_rebase()
        if not self.presence_dir.is_dir():
            return []
        now = time.time()
        rows: list[dict] = []
        for pf in self.presence_dir.glob("*.json"):
            try:
                d = json.loads(pf.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                continue
            ts = float(d.get("ts") or 0.0)
            rows.append({"identity": d.get("identity") or pf.stem, "ts": ts, "age": now - ts})
        rows.sort(key=lambda r: r["ts"], reverse=True)
        return rows
