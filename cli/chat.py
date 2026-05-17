#!/usr/bin/env python3
"""SecuredChat CLI — headless adapter for Prometheus architectures.

Subcommands:
  init     Initialize a chat room (creates room dir + chat.jsonl in the bus repo)
  send     Send a message to the room
  recv     Print all messages (or new since last seen)
  watch    Stream new messages as they arrive (polls the git bus)

The CLI never touches SecuredChat.html. It is a sibling tool that uses the
same conceptual chat room with a different transport. The HTML stays the
human-to-human WebRTC chat; this CLI is the agent-to-agent path.

See cli/README.md for the architecture and the planned aiortc upgrade path.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

from transport import GitBusTransport, Message


CONFIG_ENV_BUS = "SECUREDCHAT_BUS"
CONFIG_ENV_ROOM = "SECUREDCHAT_ROOM"
CONFIG_ENV_ID = "SECUREDCHAT_IDENTITY"


def _resolve_config(args: argparse.Namespace) -> tuple[Path, str, str]:
    bus = args.bus or os.environ.get(CONFIG_ENV_BUS)
    room = args.room or os.environ.get(CONFIG_ENV_ROOM)
    identity = args.identity or os.environ.get(CONFIG_ENV_ID)
    missing = [
        name
        for name, val in [("--bus", bus), ("--room", room), ("--identity", identity)]
        if not val
    ]
    if missing:
        sys.exit(
            f"missing config: {', '.join(missing)} "
            f"(or env: {CONFIG_ENV_BUS}, {CONFIG_ENV_ROOM}, {CONFIG_ENV_ID})"
        )
    return Path(bus), room, identity


def cmd_init(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    if t.chat_file.exists():
        print(f"room already initialized: {t.chat_file}")
        return
    t.chat_file.touch()
    rel = t.chat_file.relative_to(t.bus_repo)
    t._git("add", str(rel))
    t._git("commit", "-m", f"chat: init room {room}")
    print(f"initialized: {t.chat_file}")
    print("(remember to `git push` from the bus repo to publish)")


def cmd_send(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    body = args.body if args.body is not None else sys.stdin.read()
    if not body.strip():
        sys.exit("refusing to send empty message")
    t = GitBusTransport(bus, room, identity)
    msg = Message.new(from_=identity, to=args.to, body=body, kind=args.kind)
    t.send(msg)
    if args.json:
        print(msg.to_jsonl())
    else:
        print(f"sent {msg.id[:8]} to {args.to or 'room'}")


def cmd_recv(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    msgs = t.recv(since_id=args.since)
    if args.addressed_to_me:
        msgs = [m for m in msgs if m.to in (None, identity)]
    if args.json:
        for m in msgs:
            print(m.to_jsonl())
        return
    for m in msgs:
        prefix = f"[{m.from_}"
        if m.to:
            prefix += f"→{m.to}"
        prefix += "]"
        print(f"{prefix} {m.body}")


def cmd_watch(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    try:
        for m in t.watch(poll_seconds=args.poll):
            if args.addressed_to_me and m.to not in (None, identity):
                continue
            if args.json:
                print(m.to_jsonl(), flush=True)
            else:
                prefix = f"[{m.from_}"
                if m.to:
                    prefix += f"→{m.to}"
                prefix += "]"
                print(f"{prefix} {m.body}", flush=True)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="securedchat-cli",
        description="Headless SecuredChat adapter for Prometheus architectures.",
    )
    p.add_argument("--bus", help=f"path to git bus repo (env: {CONFIG_ENV_BUS})")
    p.add_argument("--room", help=f"room name (env: {CONFIG_ENV_ROOM})")
    p.add_argument("--identity", help=f"sender identity (env: {CONFIG_ENV_ID})")
    sub = p.add_subparsers(dest="cmd", required=True)

    s_init = sub.add_parser("init", help="initialize a chat room in the bus repo")
    s_init.set_defaults(func=cmd_init)

    s_send = sub.add_parser("send", help="send a message")
    s_send.add_argument("body", nargs="?", help="message body (or read from stdin)")
    s_send.add_argument("--to", help="recipient identity (omit = broadcast)")
    s_send.add_argument("--kind", default="msg", help="message kind (default: msg)")
    s_send.add_argument("--json", action="store_true", help="print sent message as JSONL")
    s_send.set_defaults(func=cmd_send)

    s_recv = sub.add_parser("recv", help="print messages")
    s_recv.add_argument("--since", help="only messages after this message id")
    s_recv.add_argument(
        "--addressed-to-me",
        action="store_true",
        help="filter to messages addressed to me or broadcast",
    )
    s_recv.add_argument("--json", action="store_true", help="output as JSONL")
    s_recv.set_defaults(func=cmd_recv)

    s_watch = sub.add_parser("watch", help="stream new messages as they arrive")
    s_watch.add_argument("--poll", type=float, default=5.0, help="poll interval seconds")
    s_watch.add_argument(
        "--addressed-to-me",
        action="store_true",
        help="filter to messages addressed to me or broadcast",
    )
    s_watch.add_argument("--json", action="store_true", help="output as JSONL")
    s_watch.set_defaults(func=cmd_watch)

    return p


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
