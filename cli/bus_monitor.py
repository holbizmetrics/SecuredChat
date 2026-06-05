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
    does not replay the backlog into your context. EXCEPTION: a bounded one-time
    STARTUP_SUMMARY (default last 5) surfaces UNSEEN messages that PREDATE this
    monitor — sliced against the persistent last-seen cursor (READ-only; never
    advanced) so it shows boot-step-11 semantics, not all-history. A late-started
    watcher is no longer blind to a peer's already-sent hello (the gap that caused
    the 2026-06-04 cross-session miss), and a re-arm won't re-show handled msgs.
    Summary-only (no BUS_MSG_FULL) so it never floods; --startup-summary 0 = off.
  - The monitor only *emits*. What the session DOES on a BUS_MSG (surface to
    operator, or act autonomously) is the session's policy, not the monitor's.

Usage (via the Monitor tool, persistent):
  python bus_monitor.py --bus PATH --room relay --identity windows-claude
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
    room = args.room or os.environ.get("SECUREDCHAT_ROOM") or "relay"
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


def _fmt_body(body: str | None, body_width: int) -> str:
    body = (body or "").replace("\n", " ").replace("\r", " ")
    if len(body) > body_width:
        body = body[:max(0, body_width - 3)] + "..."
    return body


def _passes(m: Message, identity: str, include_self: bool, show_all: bool) -> bool:
    """Same to-me / exclude-self filter the watch loop applies, factored so the
    startup summary surfaces exactly what a live BUS_MSG would have."""
    if not include_self and m.from_ == identity:
        return False
    if not show_all and m.to not in (None, identity):
        return False
    return True


def _saved_cursor(identity: str, room: str) -> str | None:
    """The persistent per-(identity,room) last-seen cursor, READ-ONLY. It lives
    in the chat CLI layer (~/.config/securedchat/cursors/), not transport. Best-
    effort: any failure -> None, and the startup summary falls back to all-history
    with an honest label. We only READ it; advancing the cursor stays boot-step-11
    / explicit mark-seen, so the monitor's never-writes-cursor invariant holds."""
    try:
        import chat  # CLI sibling on the same sys.path (HERE) as transport
        return chat._read_last_seen(identity, room)
    except Exception:
        return None


def emit(m: Message, body_width: int) -> None:
    print(f"BUS_MSG id={m.id[:8]} from={m.from_} kind={m.kind} "
          f"body={_fmt_body(m.body, body_width)}", flush=True)
    print(f"BUS_MSG_FULL {m.to_jsonl()}", flush=True)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bus-monitor",
        description="Read-only background bus watcher for the Claude Code Monitor tool.",
    )
    p.add_argument("--bus", help="path to git bus repo (env: SECUREDCHAT_BUS)")
    p.add_argument("--room", help="room name (env: SECUREDCHAT_ROOM; default relay)")
    p.add_argument("--identity", help="my identity (env: SECUREDCHAT_IDENTITY)")
    p.add_argument("--poll", type=float, default=30.0, help="poll interval seconds (default 30)")
    p.add_argument("--all", action="store_true",
                   help="also report messages addressed to other identities (default: only to me/broadcast)")
    p.add_argument("--include-self", action="store_true",
                   help="also report my own sent messages (default: excluded)")
    p.add_argument("--since", help="start anchor message id (default: current head — no backlog replay)")
    p.add_argument("--body-width", type=int, default=120, help="BUS_MSG body preview width (default 120)")
    p.add_argument("--startup-summary", type=int, default=5, metavar="N",
                   help="at startup, emit a one-time summary of the last N addressed-to-me "
                        "messages that predate this monitor (0 = off, default 5)")
    p.add_argument("--heartbeat", type=float, default=0.0,
                   help="also advertise presence every N seconds while watching (0 = off, default)")
    args = p.parse_args(argv)

    force_utf8_io()
    bus, room, identity = resolve(args)
    t = GitBusTransport(bus, room, identity)

    # Anchor to the current head so we report only what arrives next.
    existing: list[Message] | None = None
    if args.since is not None:
        since = args.since
    else:
        existing = t.recv(since_id=None)
        since = existing[-1].id if existing else None

    # One-time STARTUP_SUMMARY: surface a bounded window of recent messages
    # addressed to me that PREDATE this monitor. Head-anchoring (above) hides
    # them; a late-started watcher would otherwise be blind to a peer's
    # already-sent hello (the 2026-06-04 cross-session miss). Summary-only — no
    # BUS_MSG_FULL — so it never floods context; touches no cursor (recv is
    # read-only here). Distinct STARTUP_* prefixes so it's never confused with a
    # live BUS_MSG. The session still owns boot-step-11 (summary-first vs the
    # persistent cursor); this is the late-start safety net, not a replacement.
    if args.startup_summary > 0:
        if existing is None:
            existing = t.recv(since_id=None)
        # recv(since_id=None) returns ALL history, so slice to honestly-UNSEEN
        # using the persistent cursor (read-only). If a cursor resolves, label
        # the count 'unseen'; if there's no cursor or it's stale/archived (idx
        # None), fall back to all-history and label it 'addressed_before_start'
        # so the count never lies about what it counts.
        cursor = _saved_cursor(identity, room)
        unseen, label = existing, "addressed_before_start"
        if cursor:
            idx = next((i for i, m in enumerate(existing)
                        if m.id.startswith(cursor)), None)
            if idx is not None:
                unseen, label = existing[idx + 1:], "unseen"
        pending = [m for m in unseen
                   if _passes(m, identity, args.include_self, args.all)]
        recent = pending[-args.startup_summary:]
        print(f"STARTUP_SUMMARY shown={len(recent)} {label}={len(pending)}", flush=True)
        for m in recent:
            print(f"STARTUP_PENDING id={m.id[:8]} from={m.from_} kind={m.kind} "
                  f"body={_fmt_body(m.body, args.body_width)}", flush=True)

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
            if not _passes(m, identity, args.include_self, args.all):
                continue
            emit(m, args.body_width)
    except KeyboardInterrupt:
        print("MONITOR_STOPPED", file=sys.stderr)
    finally:
        stop.set()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
