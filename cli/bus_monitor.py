#!/usr/bin/env python3
"""SecuredChat bus monitor — the CLAUDE-session-facing background watcher.

Counterpart to bus_console.py (which is for a human to watch). This one is built
to be launched by the Claude Code `Monitor` tool *inside* a session, so a single
terminal Claude can react to incoming bus messages on its own — e.g. the at-home
session answering a request you sent from your phone, with nobody at that keyboard.

Emission contract (consumed by the Monitor tool; lines within ~200ms are batched
into one notification by the harness):
  - one `MONITOR_READY` line at startup
  - per new message:
        BUS_MSG id=<id8> from=<from> kind=<kind> body=<preview>
        BUS_MSG_FULL <json>

Deliberate properties:
  - READ-ONLY w.r.t. the cursor: in-memory position only; NEVER writes the
    persistent last-seen file, so it can't disturb boot-step-11 / explicit
    mark-seen. (Same safety guarantee as bus_console.py.)
  - Anchors to the CURRENT head at startup — it reports what arrives *next*, it
    does not replay the backlog into your context.
  - The monitor only *emits*. What the session DOES on a BUS_MSG (surface to
    operator, or act autonomously) is the session's policy, not the monitor's.

Usage (via the Monitor tool, persistent):
  python bus_monitor.py --bus PATH --room prometheus-relay --identity windows-claude
  # defaults: only messages addressed to me or broadcast, excluding my own.
  #   --all           also report messages addressed to other identities
  #   --include-self  also report my own sent messages
  #   --poll N        poll interval seconds (default 30)
"""
from __future__ import annotations

import argparse
import os
import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from transport import GitBusTransport, Message  # noqa: E402


def force_utf8_io() -> None:
    """UTF-8 stdout/stderr so Unicode bodies don't crash a cp1252 console."""
    for name in ("stdout", "stderr"):
        reconfigure = getattr(getattr(sys, name, None), "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def resolve(args: argparse.Namespace) -> tuple[Path, str, str]:
    bus = args.bus or os.environ.get("SECUREDCHAT_BUS")
    room = args.room or os.environ.get("SECUREDCHAT_ROOM") or "prometheus-relay"
    identity = args.identity or os.environ.get("SECUREDCHAT_IDENTITY")
    if not bus:
        sys.exit("missing --bus (or set SECUREDCHAT_BUS)")
    if not identity:
        sys.exit("missing --identity (or set SECUREDCHAT_IDENTITY)")
    return Path(bus), room, identity


def _heartbeat_loop(t: GitBusTransport, interval: float, stop: threading.Event) -> None:
    """Advertise this identity's presence every `interval` seconds while the
    monitor runs, so a watching session also shows up as 'online' to others.
    Errors are non-fatal (a flaky heartbeat must not kill the watcher)."""
    while not stop.is_set():
        try:
            t.announce_presence()
        except Exception as e:  # noqa: BLE001 — heartbeat must never crash the monitor
            print(f"securedchat: presence heartbeat error: {e}", file=sys.stderr)
        stop.wait(interval)


def emit(m: Message, body_width: int) -> None:
    body = (m.body or "").replace("\n", " ").replace("\r", " ")
    if len(body) > body_width:
        body = body[:max(0, body_width - 3)] + "..."
    print(f"BUS_MSG id={m.id[:8]} from={m.from_} kind={m.kind} body={body}", flush=True)
    print(f"BUS_MSG_FULL {m.to_jsonl()}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bus-monitor",
        description="Read-only background bus watcher for the Claude Code Monitor tool.",
    )
    p.add_argument("--bus", help="path to git bus repo (env: SECUREDCHAT_BUS)")
    p.add_argument("--room", help="room name (env: SECUREDCHAT_ROOM; default prometheus-relay)")
    p.add_argument("--identity", help="my identity (env: SECUREDCHAT_IDENTITY)")
    p.add_argument("--poll", type=float, default=30.0, help="poll interval seconds (default 30)")
    p.add_argument("--all", action="store_true",
                   help="also report messages addressed to other identities (default: only to me/broadcast)")
    p.add_argument("--include-self", action="store_true",
                   help="also report my own sent messages (default: excluded)")
    p.add_argument("--since", help="start anchor message id (default: current head — no backlog replay)")
    p.add_argument("--body-width", type=int, default=120, help="BUS_MSG body preview width (default 120)")
    p.add_argument("--heartbeat", type=float, default=0.0,
                   help="also advertise presence every N seconds while watching (0 = off, default)")
    args = p.parse_args(argv)

    force_utf8_io()
    bus, room, identity = resolve(args)
    t = GitBusTransport(bus, room, identity)

    # Anchor to the current head so we report only what arrives next.
    if args.since is not None:
        since = args.since
    else:
        existing = t.recv(since_id=None)
        since = existing[-1].id if existing else None

    stop = threading.Event()
    if args.heartbeat and args.heartbeat > 0:
        threading.Thread(target=_heartbeat_loop, args=(t, args.heartbeat, stop),
                         daemon=True).start()
        print(f"securedchat: presence heartbeat every {args.heartbeat:g}s", file=sys.stderr)

    print("MONITOR_READY", flush=True)
    try:
        # transport.watch keeps an in-memory cursor and (post-A1 fix) re-anchors
        # if the start anchor ever goes stale — it never writes the saved cursor.
        for m in t.watch(poll_seconds=args.poll, since_id=since):
            if not args.include_self and m.from_ == identity:
                continue
            if not args.all and m.to not in (None, identity):
                continue
            emit(m, args.body_width)
    except KeyboardInterrupt:
        print("MONITOR_STOPPED", file=sys.stderr)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
