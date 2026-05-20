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
        self.bus_repo = Path(bus_repo).resolve()
        self.room = room
        self.identity = identity
        self.chat_file = self.bus_repo / room / "chat.jsonl"
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

    @contextmanager
    def _send_lock(self, timeout: float = 10.0):
        """Best-effort advisory lock serializing concurrent sends on ONE machine.

        Cross-machine concurrency is still covered by push rebase-retry. If the
        lock can't be acquired within `timeout` (e.g. a crashed holder left a
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
                self._git("pull", "--rebase", "--autostash", check=False)
            with self.chat_file.open("a", encoding="utf-8") as f:
                f.write(msg.to_jsonl() + "\n")
            rel = self.chat_file.relative_to(self.bus_repo)
            add = self._git("add", str(rel), check=False)
            if add.returncode != 0:
                raise RuntimeError(f"git add failed: {add.stderr.strip()}")
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
                self._git("pull", "--rebase", "--autostash", check=False)
            raise RuntimeError(f"push failed after retries: {result.stderr if result else ''}")

    def _read_all(self) -> list[Message]:
        if not self.chat_file.exists():
            return []
        msgs: list[Message] = []
        with self.chat_file.open("r", encoding="utf-8") as f:
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

    def recv(self, since_id: str | None = None) -> list[Message]:
        if self._has_remote():
            self._git("pull", "--rebase", "--autostash", check=False)
        msgs = self._read_all()
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
            for m in self.recv(since_id=last_id):
                if m.id in seen:
                    continue
                seen.add(m.id)
                seen_order.append(m.id)
                if len(seen_order) > 2000:  # bound memory on long-running watch
                    seen.discard(seen_order.pop(0))
                last_id = m.id
                yield m
            time.sleep(poll_seconds)
