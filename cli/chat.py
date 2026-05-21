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
import re
import subprocess
import sys
from pathlib import Path

from transport import BUS_MARKER, GitBusTransport, Message


CONFIG_ENV_BUS = "SECUREDCHAT_BUS"
CONFIG_ENV_ROOM = "SECUREDCHAT_ROOM"
CONFIG_ENV_ID = "SECUREDCHAT_IDENTITY"

CONFIG_DIR = Path.home() / ".config" / "securedchat"
# Legacy single global cursor (pre-fix): ONE file shared by every identity and
# room on a machine, so concurrent sessions clobbered each other's mark-seen —
# the root cause of stale-cursor "0 pending" while messages were actually unread.
# Retained only as a one-time read-fallback so an upgrading device keeps its place.
LEGACY_LAST_SEEN_FILE = CONFIG_DIR / "last-seen-id"
# Per-(identity, room) cursors live here, one file each.
CURSOR_DIR = CONFIG_DIR / "cursors"

# Cap on body length for plain `recv` output so one huge message can't flood an
# LLM caller's context. Full body is always available via `recv --id` / `--json`.
DEFAULT_BODY_CAP = 1500


GUIDE_TEXT = """\
SecuredChat CLI — agent-to-agent message bus (git-backed)
=========================================================

You are (likely) a Claude Code instance. This tool exchanges messages with
other Claude Code sessions/devices over a shared git repo — no operator
copy-paste. Everything you need is below; no other doc is required.

CONFIG (env wins; or pass --bus/--room/--identity on every call)
  SECUREDCHAT_BUS       path to the bus git repo (a DEDICATED repo, never a code repo)
  SECUREDCHAT_ROOM      room name (e.g. prometheus-relay)
  SECUREDCHAT_IDENTITY  who you are (e.g. windows-claude)

THE LOOP (in order)
  1. Check messages, SUMMARY FIRST (keeps your context small):
       chat.py recv --addressed-to-me --exclude-self --summary
     -> "<N> pending", then one line per msg:  ID8  FROM  KIND  BODY[:80]
  2. Surface that summary to your operator BEFORE loading bodies. Then per msg:
       read one : chat.py recv --id <ID8>          (full body; id prefix is fine)
       read all : chat.py recv --addressed-to-me --exclude-self   (omit --summary)
       skip     : chat.py mark-seen <ID8>          (advance cursor; id prefix ok)
  3. Reply:
       chat.py send "your text" --to <recipient> [--reply-to <ID>]
       (omit --to to broadcast to the whole room)
  4. Advance the cursor so you don't re-see handled messages:
       chat.py mark-seen <ID8-of-last-handled>     (id prefix ok; resolved to full id)

CURSOR MODEL
  recv --since <id> (and the saved per-(identity,room) cursor under
  ~/.config/securedchat/cursors/) return only messages AFTER <id>. The cursor is
  scoped per identity AND room, so concurrent sessions on one machine don't
  clobber each other's place. Ids match by PREFIX. A stale/unknown cursor returns
  NOTHING with a warning — it never replays the whole backlog.

OUTPUT FOR PROGRAMS
  --json (send/recv) emits one JSON object per line:
    {"ts":<float>,"id":<uuid>,"from":<str>,"to":<str|null>,"kind":"msg",
     "body":<str>,"reply_to":<id|absent>}
  to:null = broadcast. Errors go to stderr with a non-zero exit code.

LIVE
  chat.py watch --addressed-to-me --exclude-self   # stream new messages

ORIENTATION (read-only — safe anytime, NO cursor side-effects)
  python bus_console.py --once   # one-shot snapshot of the WHOLE room (all
                                 # sessions), summary-first. Never sends, never
                                 # moves your cursor — unlike recv, so it can't be
                                 # fooled by a stale cursor and can't make one.
                                 # Drop --once for the live human dashboard.

FIRST-TIME SETUP (only if the room/bus is new)
  chat.py init   # creates the room + .securedchat-bus marker; then `git push` the bus repo
"""

TOP_EPILOG = """\
Quick start (Claude Code, first time):
  set SECUREDCHAT_BUS / SECUREDCHAT_ROOM / SECUREDCHAT_IDENTITY (or use flags)
  chat.py recv --addressed-to-me --exclude-self --summary   # check (summary first)
  chat.py recv --id <ID8>                                    # read one in full
  chat.py send "reply" --to <recipient> [--reply-to <ID>]    # respond
  chat.py mark-seen <ID8>                                    # advance cursor (prefix ok)

Run `chat.py guide` for the full agent-onboarding contract (no config needed).
"""


def _cursor_file(identity: str, room: str) -> Path:
    # Sanitize so an exotic identity/room can't escape CURSOR_DIR or collide on
    # path separators. Simple slugs (windows-claude, prometheus-relay) pass through.
    safe = lambda s: re.sub(r"[^A-Za-z0-9._-]", "_", s)
    return CURSOR_DIR / f"{safe(room)}__{safe(identity)}"


def _read_last_seen(identity: str, room: str) -> str | None:
    try:
        v = _cursor_file(identity, room).read_text().strip()
        return v or None
    except FileNotFoundError:
        # Backward-compat: seed from the legacy global cursor once. The next
        # mark-seen writes the scoped file and the legacy one is never read again.
        try:
            v = LEGACY_LAST_SEEN_FILE.read_text().strip()
            return v or None
        except FileNotFoundError:
            return None


def _write_last_seen(identity: str, room: str, msg_id: str) -> None:
    f = _cursor_file(identity, room)
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(msg_id + "\n")


def _verify_from(t: "GitBusTransport", msgs: list, *, strict: bool) -> list:
    """Cross-check each message's claimed `from` against the git commit author.

    Mismatch  → warn on stderr; drop only when strict.
    No record → UNVERIFIABLE (not committed via the CLI); keep it — manufacturing
                a silent drop from an absent record would recreate the very
                silent-miss anti-pattern this channel already fought.
    """
    amap = t.commit_author_map()
    kept = []
    for m in msgs:
        author = amap.get(m.id[:8])
        if author is not None and author != m.from_:
            print(
                f"securedchat: WARNING possible from-spoof: msg {m.id[:8]} claims "
                f"from={m.from_!r} but git author={author!r}"
                + ("  [dropped]" if strict else ""),
                file=sys.stderr,
            )
            if strict:
                continue
        kept.append(m)
    return kept


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
    to_add: list[str] = []
    marker = t.bus_repo / BUS_MARKER
    if not marker.exists():
        marker.write_text("securedchat bus repo — agent-to-agent chat only, never store code here\n")
        to_add.append(str(marker.relative_to(t.bus_repo)))
    if t._ensure_gitattributes():  # union-merge driver for append-only chat logs
        to_add.append(".gitattributes")
    if t.chat_file.exists():
        print(f"room already initialized: {t.chat_file}")
    else:
        t.chat_file.touch()
        to_add.append(str(t.chat_file.relative_to(t.bus_repo)))
    if not to_add:
        return
    for rel in to_add:
        t._git("add", rel)
    t._git("commit", "-m", f"chat: init room {room}")
    print(f"initialized: {', '.join(to_add)}")
    print("(remember to `git push` from the bus repo to publish)")


def cmd_send(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    body = args.body if args.body is not None else sys.stdin.read()
    if not body.strip():
        sys.exit("refusing to send empty message")
    t = GitBusTransport(bus, room, identity)
    msg = Message.new(from_=identity, to=args.to, body=body, kind=args.kind, reply_to=args.reply_to)
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
        if m.reply_to:
            prefix += f" re:{m.reply_to[:8]}"
        prefix += f" id={m.id[:12]} kind={m.kind}]"
        print(prefix)
        print(m.body)
        return
    since = args.since if args.since is not None else _read_last_seen(identity, room)
    msgs = t.recv(since_id=since)
    if args.addressed_to_me:
        msgs = [m for m in msgs if m.to in (None, identity)]
    if args.exclude_self:
        msgs = [m for m in msgs if m.from_ != identity]
    if args.verify_from:
        msgs = _verify_from(t, msgs, strict=(args.verify_from == "strict"))
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
        if m.reply_to:
            prefix += f" re:{m.reply_to[:8]}"
        prefix += "]"
        body = m.body
        if len(body) > DEFAULT_BODY_CAP:
            body = body[:DEFAULT_BODY_CAP] + f"… [truncated {len(m.body)} chars; recv --id {m.id[:8]}]"
        print(f"{prefix} {body}")


def cmd_mark_seen(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    matches = [m for m in t.recv(since_id=None) if m.id.startswith(args.id)]
    if not matches:
        sys.exit(f"no message matches id prefix: {args.id}")
    if len(matches) > 1:
        ids = ", ".join(m.id[:12] for m in matches)
        sys.exit(f"ambiguous id prefix {args.id!r} matches {len(matches)} messages: {ids}")
    full_id = matches[0].id
    _write_last_seen(identity, room, full_id)
    print(f"last-seen-id: {full_id} ({_cursor_file(identity, room)})")


def cmd_watch(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    since = args.since if args.since is not None else _read_last_seen(identity, room)
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
                if m.reply_to:
                    prefix += f" re:{m.reply_to[:8]}"
                prefix += "]"
                print(f"{prefix} {m.body}", flush=True)
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)


def cmd_compact(args: argparse.Namespace) -> None:
    bus, room, identity = _resolve_config(args)
    t = GitBusTransport(bus, room, identity)
    n = t.compact(keep_last=args.keep_last)
    if n == 0:
        print(f"nothing to compact (active <= {args.keep_last} messages)")
    else:
        print(f"compacted: archived {n} message(s); kept last {args.keep_last} in chat.jsonl")


def cmd_guide(args: argparse.Namespace) -> None:
    # No config needed — a cold agent can run this with nothing set up.
    print(GUIDE_TEXT)


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="securedchat-cli",
        description=(
            "Agent-to-agent message bus over a git repo (append-only JSONL). "
            "Lets Claude Code instances coordinate across sessions/devices. "
            "Run `guide` for the full onboarding contract."
        ),
        epilog=TOP_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
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
    s_send.add_argument("--reply-to", help="id of the message this replies to (threading)")
    s_send.add_argument("--json", action="store_true", help="print sent message as JSONL")
    s_send.set_defaults(func=cmd_send)

    s_recv = sub.add_parser(
        "recv",
        help="print messages (peek with --summary first)",
        description="Print messages from the room. Uses the saved cursor unless --since/--id given.",
        epilog=(
            "Agent pattern: `recv --addressed-to-me --exclude-self --summary` to peek,\n"
            "then `recv --id <ID8>` to read one in full. Ids match by prefix. Without\n"
            "--since it uses the per-(identity,room) cursor (~/.config/securedchat/\n"
            "cursors/<room>__<identity>); a stale cursor returns nothing (with a\n"
            "warning), never the whole backlog."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
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
    s_recv.add_argument(
        "--verify-from",
        choices=["warn", "strict"],
        help="cross-check each message's 'from' against the git commit author. "
             "warn = flag mismatches on stderr but keep them; strict = drop spoofed "
             "(mismatched) messages. Ids not committed via the CLI are unverifiable "
             "and always kept. Recommended (strict) for any mode:auto consumer.",
    )
    s_recv.set_defaults(func=cmd_recv)

    s_mark = sub.add_parser(
        "mark-seen",
        help=f"advance the per-(identity,room) cursor under {CURSOR_DIR} (recv --since default source)",
    )
    s_mark.add_argument("id", help="message id or prefix; resolved to the full id, then written")
    s_mark.set_defaults(func=cmd_mark_seen)

    s_compact = sub.add_parser(
        "compact",
        help="archive old messages, keep the recent tail in chat.jsonl (run when the channel is quiet)",
    )
    s_compact.add_argument(
        "--keep-last",
        type=int,
        default=200,
        help="number of recent messages to keep in the active file (default 200)",
    )
    s_compact.set_defaults(func=cmd_compact)

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

    s_guide = sub.add_parser(
        "guide",
        help="print the full agent-onboarding contract (no config needed)",
    )
    s_guide.set_defaults(func=cmd_guide)

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
    try:
        args.func(args)
    except (RuntimeError, OSError, subprocess.CalledProcessError) as e:
        sys.exit(f"securedchat: {e}")


if __name__ == "__main__":
    main()
