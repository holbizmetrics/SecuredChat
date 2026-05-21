#!/usr/bin/env python3
"""SecuredChat bus console — a live, read-only dashboard for the HUMAN operator.

This is the one piece of the CLI built for a person to *watch* rather than for an
agent to drive. `send`/`recv`/`watch`/`mark-seen` are the Claude-to-Claude
plumbing; this is your window onto the channel: a full-screen, auto-refreshing
list of bus traffic where you press a row number to expand the full message
("show me the full one, please"), toggle filters, and quit.

Deliberate properties:
  - READ-ONLY. It never sends and never writes the persistent last-seen cursor
    (~/.config/securedchat/cursors/...), so watching does NOT disturb what the
    agent sessions track. It keeps its own in-memory position.
  - Stdlib only, cross-platform (msvcrt on Windows; select+termios on Unix).
  - Reuses SECUREDCHAT_BUS / SECUREDCHAT_ROOM / SECUREDCHAT_IDENTITY (or flags).

Usage:
  python bus_console.py                 # uses env config
  python bus_console.py --bus PATH --room prometheus-relay --identity windows-claude
  python bus_console.py --me            # start filtered to messages addressed to me
  python bus_console.py --once          # render one frame and exit (non-interactive)

Keys:  [#]=open row   a=toggle me-filter   /=text filter   r=refresh   q=quit
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

from transport import GitBusTransport, Message  # noqa: E402

IS_WIN = os.name == "nt"
if IS_WIN:
    import msvcrt
else:
    import select
    import termios
    import tty


# --------------------------------------------------------------------------- #
# Terminal helpers
# --------------------------------------------------------------------------- #
def force_utf8_io() -> None:
    """Force stdout/stderr to UTF-8 so Unicode message bodies (arrows, math,
    emoji, non-Latin scripts) don't crash a cp1252 Windows console. Mirrors
    chat.py's _force_utf8_io; errors='replace' degrades instead of raising."""
    for name in ("stdout", "stderr"):
        stream = getattr(sys, name, None)
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8", errors="replace")
            except Exception:
                pass


def enable_ansi() -> None:
    """Enable ANSI escape processing on Windows 10+ consoles (no-op elsewhere)."""
    if IS_WIN:
        try:
            import ctypes
            k = ctypes.windll.kernel32
            # ENABLE_PROCESSED_OUTPUT|ENABLE_WRAP_AT_EOL_OUTPUT|ENABLE_VT_PROCESSING
            k.SetConsoleMode(k.GetStdHandle(-11), 7)
        except Exception:
            pass


def cls() -> None:
    sys.stdout.write("\x1b[2J\x1b[H")
    sys.stdout.flush()


class KeyReader:
    """Cross-platform single-key reader. Cbreak on Unix (restored on exit);
    msvcrt on Windows. Used for both timed polling and blocking line entry."""

    def __enter__(self) -> "KeyReader":
        self._old = None
        if not IS_WIN and sys.stdin.isatty():
            self._fd = sys.stdin.fileno()
            self._old = termios.tcgetattr(self._fd)
            tty.setcbreak(self._fd)
        return self

    def __exit__(self, *exc) -> None:
        if self._old is not None:
            termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old)

    def get(self, timeout: float) -> str | None:
        """One key within `timeout` seconds, else None."""
        if IS_WIN:
            end = time.time() + timeout
            while time.time() < end:
                if msvcrt.kbhit():
                    return msvcrt.getwch()
                time.sleep(0.03)
            return None
        r, _, _ = select.select([sys.stdin], [], [], timeout)
        return sys.stdin.read(1) if r else None

    def get_blocking(self) -> str:
        if IS_WIN:
            return msvcrt.getwch()
        return sys.stdin.read(1)

    def prompt_line(self, prefix: str = "") -> str:
        """Read a line with minimal echo/backspace, starting from `prefix`.
        Works in cbreak/msvcrt mode so we don't have to toggle terminal modes."""
        buf = list(prefix)
        sys.stdout.write(prefix)
        sys.stdout.flush()
        while True:
            ch = self.get_blocking()
            if IS_WIN and ch in ("\x00", "\xe0"):  # function/arrow key — swallow code
                self.get_blocking()
                continue
            if ch in ("\r", "\n"):
                sys.stdout.write("\n")
                sys.stdout.flush()
                return "".join(buf)
            if ch in ("\x7f", "\x08"):  # backspace
                if buf:
                    buf.pop()
                    sys.stdout.write("\b \b")
                    sys.stdout.flush()
                continue
            if ch == "\x03":  # Ctrl-C
                raise KeyboardInterrupt
            if ch.isprintable():
                buf.append(ch)
                sys.stdout.write(ch)
                sys.stdout.flush()


# --------------------------------------------------------------------------- #
# View model
# --------------------------------------------------------------------------- #
def _age(seconds: float) -> str:
    s = int(max(0, seconds))
    if s < 60:
        return f"{s}s"
    if s < 3600:
        return f"{s // 60}m"
    return f"{s // 3600}h"


def online_str(t: GitBusTransport, window: float = 300.0) -> str | None:
    """Identities seen within `window` seconds, as 'id(age) id(age) ...'. Reads
    presence WITHOUT pulling (the dashboard's recv already refreshed the repo)."""
    try:
        pres = t.read_presence(pull=False)
    except Exception:
        return None
    live = [r for r in pres if r["age"] <= window]
    return "  ".join(f"{r['identity']}({_age(r['age'])})" for r in live) if live else "none"


def apply_filter(messages: list[Message], identity: str, me_only: bool, text: str) -> list[Message]:
    out = messages
    if me_only:
        out = [m for m in out if m.to in (None, identity)]
    if text:
        tl = text.lower()
        out = [m for m in out
               if tl in (m.from_ or "").lower() or tl in (m.to or "").lower()
               or tl in (m.body or "").lower() or tl in (m.kind or "").lower()]
    return out


def render(messages, *, identity, room, me_only, text_filter, new_ids, poll, cols, rows, online=None):
    """Return (frame_str, shown_list, start_index). shown_list[i] is the message
    displayed at row number start_index+i."""
    fmsgs = apply_filter(messages, identity, me_only, text_filter)
    visible = max(3, rows - 5)
    shown = fmsgs[-visible:]
    start_index = len(fmsgs) - len(shown) + 1 if fmsgs else 1

    flt = "me" if me_only else (f"/{text_filter}" if text_filter else "all")
    new_n = sum(1 for m in fmsgs if m.id in new_ids)
    head = (f"{room}   me: {identity}   filter: {flt}   * live {poll:g}s   "
            f"({len(fmsgs)} msgs{', ' + str(new_n) + ' new' if new_n else ''})")
    lines = [head[:cols]]
    if online is not None:
        lines.append(f"online: {online}"[:cols])
    lines.append("-" * cols)

    for i, m in enumerate(shown, start=start_index):
        is_new = m.id in new_ids
        t = time.strftime("%H:%M", time.localtime(m.ts)) if m.ts else "--:--"
        frm = (m.from_ or "?")[:13]
        to = (m.to or "(all)")[:9]
        kind = (m.kind or "")[:7]
        mark = "*" if is_new else " "
        prefix = f"{i:>3}{mark} {t}  {frm:<13}-> {to:<9} {kind:<7} "
        suffix = "  <- NEW" if is_new else ""
        body = (m.body or "").replace("\n", " ").replace("\r", " ")
        avail = max(0, cols - len(prefix) - len(suffix))
        if len(body) > avail:
            body = body[:max(0, avail - 3)] + "..."
        lines.append((prefix + body + suffix)[:cols])

    lines.append("-" * cols)
    lines.append("[#]=open  a=toggle me  /=filter  r=refresh  q=quit"[:cols])
    return "\n".join(lines), shown, start_index


def show_detail(m: Message, kr: KeyReader) -> None:
    cls()
    hdr = f"[{m.from_}" + (f"->{m.to}" if m.to else " ->(all)")
    if m.reply_to:
        hdr += f" re:{m.reply_to[:8]}"
    hdr += f" id={m.id} kind={m.kind}]"
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(m.ts)) if m.ts else "?"
    print(hdr)
    print(ts)
    print("-" * 60)
    print(m.body)
    print("-" * 60)
    print("[any key to return]")
    kr.get_blocking()


def show_help(kr: KeyReader) -> None:
    cls()
    print("SecuredChat bus console — keys\n")
    print("  <number>   open that row and show the FULL message body")
    print("  a          toggle 'addressed to me' filter")
    print("  /          set a text filter (matches from/to/kind/body); empty clears")
    print("  r / Enter  refresh now")
    print("  q          quit")
    print("\nRead-only: never sends, never moves the agents' saved cursor.")
    print("\n[any key to return]")
    kr.get_blocking()


# --------------------------------------------------------------------------- #
def resolve(args: argparse.Namespace) -> tuple[Path, str, str]:
    bus = args.bus or os.environ.get("SECUREDCHAT_BUS")
    room = args.room or os.environ.get("SECUREDCHAT_ROOM") or "prometheus-relay"
    identity = args.identity or os.environ.get("SECUREDCHAT_IDENTITY") or "viewer"
    if not bus:
        sys.exit("missing --bus (or set SECUREDCHAT_BUS)")
    return Path(bus), room, identity


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        prog="bus-console",
        description="Live, read-only human dashboard for the SecuredChat git bus.",
    )
    p.add_argument("--bus", help="path to git bus repo (env: SECUREDCHAT_BUS)")
    p.add_argument("--room", help="room name (env: SECUREDCHAT_ROOM; default prometheus-relay)")
    p.add_argument("--identity", help="your identity for the me-filter (env: SECUREDCHAT_IDENTITY)")
    p.add_argument("--poll", type=float, default=3.0, help="refresh interval seconds (default 3)")
    p.add_argument("--me", action="store_true", help="start filtered to messages addressed to me")
    p.add_argument("--once", action="store_true", help="render one frame and exit (non-interactive)")
    args = p.parse_args(argv)

    force_utf8_io()
    bus, room, identity = resolve(args)
    t = GitBusTransport(bus, room, identity)

    messages: list[Message] = t.recv(since_id=None)
    last_id = messages[-1].id if messages else None
    me_only = args.me
    text_filter = ""
    new_ids: set[str] = set()
    cols, rows = shutil.get_terminal_size((100, 30))

    if args.once:
        frame, _, _ = render(messages, identity=identity, room=room, me_only=me_only,
                             text_filter=text_filter, new_ids=new_ids, poll=args.poll,
                             cols=cols, rows=rows, online=online_str(t))
        print(frame)
        return 0

    enable_ansi()
    try:
        with KeyReader() as kr:
            while True:
                # Poll for new messages (incremental once we have an anchor).
                try:
                    if last_id is None:
                        cur = t.recv(since_id=None)
                        if cur:
                            new_ids.update(m.id for m in cur)
                            messages = cur
                            last_id = cur[-1].id
                    else:
                        fresh = t.recv(since_id=last_id)
                        if fresh:
                            new_ids.update(m.id for m in fresh)
                            messages.extend(fresh)
                            last_id = messages[-1].id
                except Exception as e:  # keep the dashboard alive on a transient git error
                    sys.stderr.write(f"\n(poll error: {e})\n")

                cols, rows = shutil.get_terminal_size((100, 30))
                cls()
                frame, shown, start = render(
                    messages, identity=identity, room=room, me_only=me_only,
                    text_filter=text_filter, new_ids=new_ids, poll=args.poll,
                    cols=cols, rows=rows, online=online_str(t))
                sys.stdout.write(frame)
                sys.stdout.flush()

                c = kr.get(args.poll)
                if c is None:
                    continue
                if c in ("q", "Q", "\x03"):
                    break
                new_ids.clear()  # any interaction = "I've looked"
                if c in ("r", "R", "\r", "\n"):
                    continue
                if c in ("a", "A"):
                    me_only = not me_only
                    continue
                if c in ("h", "H", "?"):
                    show_help(kr)
                    continue
                if c == "/":
                    text_filter = kr.prompt_line("/")[1:].strip()
                    continue
                if IS_WIN and c in ("\x00", "\xe0"):
                    kr.get_blocking()  # swallow function-key code
                    continue
                if c.isdigit():
                    s = kr.prompt_line(c)
                    if s.isdigit() and shown:
                        idx = int(s) - start
                        if 0 <= idx < len(shown):
                            show_detail(shown[idx], kr)
                    continue
                # any other key: ignore, loop redraws
    except KeyboardInterrupt:
        pass
    finally:
        cls()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
