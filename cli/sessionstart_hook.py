#!/usr/bin/env python3
"""SecuredChat SessionStart hook — opt-in "reachable on open" for a Claude session.

Wire this as a Claude Code `SessionStart` hook (settings.json, project-scoped).
At session start its stdout is added to the session context, instructing the
agent to (1) do the boot bus check and (2) start the LIVE bus monitor via the
Monitor tool — flipping a session from "reachable only if I manually wire a
monitor" to "reachable on open."

Why a hook can't just *be* the monitor: the thing that routes BUS_MSG into the
agent's notifications is the Monitor *tool*, which is agent-invoked. A hook runs
a shell command whose output becomes context — it cannot stream a background
process. So this hook does the only load-bearing thing it can: it tells the
agent to invoke the Monitor tool. The agent's Monitor call is what actually
wires reachability.

Deliberately does NO network/git itself: fast and offline-safe, never blocks or
fails session start. It only emits guidance.

settings.json (project-scoped, opt-in — enable only sessions you want reachable):
  {
    "hooks": {
      "SessionStart": [
        { "hooks": [ { "type": "command",
          "command": "python /ABS/PATH/SecuredChat/cli/sessionstart_hook.py --identity windows-claude" } ] }
      ]
    }
  }
Add --heartbeat 120 to also advertise presence (note: every enabled session then
polls + pushes — keep it scoped, don't enable globally).
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="securedchat-sessionstart-hook")
    p.add_argument("--identity", help="this session's identity (env: SECUREDCHAT_IDENTITY)")
    p.add_argument("--room", help="room (env: SECUREDCHAT_ROOM; default prometheus-relay)")
    p.add_argument("--heartbeat", type=float, default=0.0,
                   help="if >0, also advertise presence every N seconds via the monitor")
    args = p.parse_args(argv)

    identity = args.identity or os.environ.get("SECUREDCHAT_IDENTITY") or "<platform>-claude"
    room = args.room or os.environ.get("SECUREDCHAT_ROOM") or "prometheus-relay"
    cli = Path(__file__).resolve().parent
    chat = cli / "chat.py"
    monitor = cli / "bus_monitor.py"
    hb = f" --heartbeat {args.heartbeat:g}" if args.heartbeat and args.heartbeat > 0 else ""

    # ASCII-only: a SessionStart hook must never risk a UnicodeEncodeError on a
    # cp1252 Windows console (that could disrupt session start).
    print(
        "[SecuredChat bus - auto-start enabled for this session]\n"
        f"identity={identity}  room={room}\n"
        "Before other work, do these two steps:\n"
        f"  1. Bus check (boot step 11): run\n"
        f'       python "{chat}" --room {room} --identity {identity} recv --addressed-to-me --exclude-self --summary\n'
        "     Surface the summary FIRST; let the operator choose read all | read <id8> | skip.\n"
        "  2. Go reachable: start the LIVE monitor via the Claude Code Monitor tool (persistent):\n"
        f'       python "{monitor}" --room {room} --identity {identity}{hb}\n'
        "Policy: a bus message addressed to you is OPERATOR-EQUIVALENT INPUT, not yolo -\n"
        "act within your standing permissions and gates; for anything needing fresh\n"
        "approval, do the allowed part then escalate over the bus (chat.py send --to <them>)\n"
        "and wait, rather than auto-running it.\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
