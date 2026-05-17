"""Transport layer for SecuredChat CLI.

Defines a Transport abstraction so the CLI can swap out delivery mechanisms
without changing the user-facing commands. Today: GitBusTransport (append-
only JSONL in a private git repo, push/pull as the sync primitive). Tomorrow:
WebRTCTransport (aiortc data channel, with SDP handshake bootstrapped over
the git bus).

Message wire format (JSONL line):

    {"ts": <unix-float>, "id": <uuid>, "from": <str>,
     "to": <str-or-null>, "kind": "msg", "body": <str>}

`to: null` = broadcast within the room. `kind` reserved for future control
frames (sdp-offer, sdp-answer, presence, etc.).
"""
from __future__ import annotations

import json
import os
import subprocess
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterator


@dataclass
class Message:
    ts: float
    id: str
    from_: str
    to: str | None
    kind: str
    body: str

    @classmethod
    def new(cls, from_: str, to: str | None, body: str, kind: str = "msg") -> "Message":
        return cls(
            ts=time.time(),
            id=str(uuid.uuid4()),
            from_=from_,
            to=to,
            kind=kind,
            body=body,
        )

    def to_jsonl(self) -> str:
        d = asdict(self)
        d["from"] = d.pop("from_")
        return json.dumps(d, ensure_ascii=False)

    @classmethod
    def from_jsonl(cls, line: str) -> "Message":
        d = json.loads(line)
        d["from_"] = d.pop("from")
        return cls(**d)


class Transport(ABC):
    @abstractmethod
    def send(self, msg: Message) -> None: ...

    @abstractmethod
    def recv(self, since_id: str | None = None) -> list[Message]: ...

    @abstractmethod
    def watch(self, poll_seconds: float = 5.0) -> Iterator[Message]: ...


class GitBusTransport(Transport):
    """Append-only JSONL in a git repo. Push on send, pull on recv.

    Constraints:
    - Designed for low-frequency exchange (agent-to-agent relay, not chatty).
    - Race-prone if two senders push concurrently; rebase-on-push retry handles
      the common case. Higher contention needs aiortc.
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

    def send(self, msg: Message) -> None:
        if self._has_remote():
            self._git("pull", "--rebase", "--autostash", check=False)
        with self.chat_file.open("a", encoding="utf-8") as f:
            f.write(msg.to_jsonl() + "\n")
        rel = self.chat_file.relative_to(self.bus_repo)
        self._git("add", str(rel))
        self._git(
            "-c", f"user.email={self.identity}@securedchat-cli",
            "-c", f"user.name={self.identity}",
            "commit", "-m", f"chat: {self.room} {msg.id[:8]}",
        )
        if not self._has_remote():
            return
        for attempt in range(3):
            result = self._git("push", check=False)
            if result.returncode == 0:
                return
            self._git("pull", "--rebase", "--autostash", check=False)
        raise RuntimeError(f"push failed after retries: {result.stderr}")

    def recv(self, since_id: str | None = None) -> list[Message]:
        if self._has_remote():
            self._git("pull", "--rebase", "--autostash", check=False)
        if not self.chat_file.exists():
            return []
        msgs: list[Message] = []
        with self.chat_file.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    msgs.append(Message.from_jsonl(line))
                except (json.JSONDecodeError, TypeError, KeyError):
                    continue
        if since_id is None:
            return msgs
        for i, m in enumerate(msgs):
            if m.id == since_id:
                return msgs[i + 1 :]
        return msgs

    def watch(self, poll_seconds: float = 5.0) -> Iterator[Message]:
        last_id: str | None = None
        seen: set[str] = set()
        while True:
            for m in self.recv(since_id=last_id):
                if m.id in seen:
                    continue
                seen.add(m.id)
                last_id = m.id
                yield m
            time.sleep(poll_seconds)
