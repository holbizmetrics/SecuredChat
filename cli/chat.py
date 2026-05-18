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

LAST_SEEN_FILE = Path.home() / ".config" / "securedchat" / "last-seen-id"


def _read_last_seen() -> str | None:
    try:
        v = LAST_SEEN_FILE.read_text().strip()
        return v or None
    except FileNotFoundError:
        return None


def _write_last_seen(msg_id: str) -> None:
    LAST_SEEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    LAST_SEEN_FILE.write_text(msg_id + "\n")


def _summary_line(m: "Message", body_width: int) -> str:
    body = m.body.replace("\n", " ").replace("\r", " ")
    if len(body) > body_width:
        body = body[: body_width - 1] + "…"
    return f"{m.id[:8]}  {m.from_:<16}  {m.kind:<10}  {body}"


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
    if args.id:
        # --id: fetch one specific message by id (full or prefix). Bypasses
        # --since / --addressed-to-me / --exclude-self filters — when you have
        # a specific id you want THAT message regardless. Recovery path for
        # truncated previews surfaced by upstream monitors.
        all_msgs = t.recv(since_id=None)
        matches = [m for m in all_msgs if m.id.startswith(args.id)]
        if not matches:
            sys.exit(f"no message matches id prefix: {args.id}")
        if len(matches) > 1:
            ids = ", ".join(m.id[:12] for m in matches)
            sys.exit(f"ambiguous id prefix {args.id!r} matches {len(matches)} messages: {ids}")
        m = matches[0]
        if args.json:
            print(m.to_jsonl())
            return
        prefix = f"[{m.from_}"
        if m.to:
            prefix += f"→{m.to}"
        prefix += f" id={m.id[:12]} kind={m.kind}]"
        print(prefix)
        print(m.body)
        return
    since = args.since if args.since is not None else _read_last_seen()
    msgs = t.recv(since_id=since)
    if args.addressed_to_me:
        msgs = [m for m in msgs if m.to in (None, identity)]
    if args.exclude_self:
        msgs = [m for m in msgs if m.from_ != identity]
    if args.summary:
        print(f"{len(msgs)} pending")
        for m in msgs:
            print(_summary_line(m, args.summary_width))
        return
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


def cmd_mark_seen(args: argparse.Namespace) -> None:
    _write_last_seen(args.id)
    print(f"last-seen-id: {args.id} ({LAST_SEEN_FILE})")


def cmd_watch(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    since = args.since if args.since is not None else _read_last_seen()
    try:
        for m in t.watch(poll_seconds=args.poll, since_id=since):
            if args.addressed_to_me and m.to not in (None, identity):
                continue
            if args.exclude_self and m.from_ == identity:
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
    s_recv.add_argument(
        "--id",
        help="fetch a single message by full id or prefix (recovery path for "
             "truncated previews; bypasses --since / --addressed-to-me / --exclude-self)",
    )
    s_recv.add_argument("--since", help="only messages after this message id")
    s_recv.add_argument(
        "--addressed-to-me",
        action="store_true",
        help="filter to messages addressed to me or broadcast",
    )
    s_recv.add_argument(
        "--exclude-self",
        action="store_true",
        help="skip messages where from == this identity (suppress self-echo)",
    )
    s_recv.add_argument(
        "--summary",
        action="store_true",
        help="one-line-per-message: '<count> pending' then 'ID8 FROM KIND BODY[:W]'",
    )
    s_recv.add_argument(
        "--summary-width",
        type=int,
        default=80,
        help="body preview width for --summary (default 80)",
    )
    s_recv.add_argument("--json", action="store_true", help="output as JSONL")
    s_recv.set_defaults(func=cmd_recv)

    s_mark = sub.add_parser(
        "mark-seen",
        help=f"write message id to {LAST_SEEN_FILE} (recv --since default source)",
    )
    s_mark.add_argument("id", help="full message id (must match exactly; recv uses == comparison)")
    s_mark.set_defaults(func=cmd_mark_seen)

    s_watch = sub.add_parser("watch", help="stream new messages as they arrive")
    s_watch.add_argument("--poll", type=float, default=5.0, help="poll interval seconds")
    s_watch.add_argument("--since", help="start after this message id (skip backlog)")
    s_watch.add_argument(
        "--addressed-to-me",
        action="store_true",
        help="filter to messages addressed to me or broadcast",
    )
    s_watch.add_argument(
        "--exclude-self",
        action="store_true",
        help="skip messages where from == this identity (suppress self-echo)",
    )
    s_watch.add_argument("--json", action="store_true", help="output as JSONL")
    s_watch.set_defaults(func=cmd_watch)

    return p


def _force_utf8_io() -> None:
    """Force stdout/stderr to UTF-8 on Windows.

    Bus messages contain arbitrary Unicode (arrows in identity prefixes,
    emoji, math symbols, non-Latin scripts). Windows defaults stdout to
    cp1252, which raises UnicodeEncodeError on the first non-encodable
    character. This makes the CLI usable cross-platform without requiring
    callers to set PYTHONIOENCODING=utf-8.
    """
    for stream_name in ("stdout", "stderr"):
        stream = getattr(sys, stream_name, None)
        if stream is None:
            continue
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


def main(argv: list[str] | None = None) -> None:
    _force_utf8_io()
    args = build_parser().parse_args(argv)
    args.func(args)


if __name__ == "__main__":
    main()
