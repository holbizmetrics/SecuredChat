#!/usr/bin/env python3
"""test_presence_title.py — `presence --title` / `--find`: resolve an operator's window name.

Why (2026-11-01): a phone session addressed two messages to 'pcla-work', a bus identity dead for
57 days, because all it knew was the operator's Claude Code chat title 'pcla-works'. The window it
meant was beating presence as windows-claude-b118ae6a the whole time. A title carried in the
presence file turns that guess into a lookup.

Every positive has its negative: --find must MISS (exit 1) for an unknown title and for a session
that never set one, or it would answer for windows it cannot actually see.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
from transport import BUS_MARKER, GitBusTransport  # noqa: E402

FAILS = []
CHECKS = 0


def check(cond, label):
    global CHECKS
    CHECKS += 1
    print(("PASS " if cond else "FAIL ") + label)
    if not cond:
        FAILS.append(label)


def make_bus(root):
    repo = root / "bus"
    repo.mkdir()
    subprocess.run(["git", "init", "-q", str(repo)], check=True)
    for k, v in (("user.email", "t@t"), ("user.name", "t")):
        subprocess.run(["git", "-C", str(repo), "config", k, v], check=True)
    (repo / BUS_MARKER).write_text("", encoding="utf-8")   # a real bus, so the CLI does not warn
    return repo


def cli(repo, identity, *args):
    env = dict(os.environ, SECUREDCHAT_BUS=str(repo), SECUREDCHAT_ROOM="relay",
               SECUREDCHAT_IDENTITY=identity, PYTHONIOENCODING="utf-8")
    return subprocess.run([sys.executable, str(HERE / "chat.py"), "presence", *args],
                          capture_output=True, text=True, encoding="utf-8", env=env)


def main():
    with tempfile.TemporaryDirectory() as td:
        repo = make_bus(Path(td))

        # transport layer: the title round-trips; an untitled beat reads back as None
        GitBusTransport(repo, "relay", "windows-claude-aaaa").announce_presence({"title": "pcla-works"})
        GitBusTransport(repo, "relay", "termux-claude-bbbb").announce_presence()
        rows = {r["identity"]: r for r in GitBusTransport(repo, "relay", "x").read_presence(pull=False)}
        check(rows["windows-claude-aaaa"]["title"] == "pcla-works", "a titled beat reads back its title")
        check(rows["termux-claude-bbbb"]["title"] is None, "an untitled beat reads back title=None")
        raw = json.loads((repo / "relay" / "presence" / "windows-claude-aaaa.json").read_text(encoding="utf-8"))
        check(raw.get("identity") == "windows-claude-aaaa" and "ts" in raw,
              "the title is added beside identity/ts, not instead of them")

        # CLI: --find hits by title (case-insensitive, whitespace-normalised) ...
        p = cli(repo, "reader", "--find", "  PCLA-Works ")
        check(p.returncode == 0 and "windows-claude-aaaa" in p.stdout, "--find resolves the title to its identity")
        # ... and MISSES loudly, never answering for a window it cannot see
        p = cli(repo, "reader", "--find", "pcla-work")
        check(p.returncode == 1 and "windows-claude" not in p.stdout,
              "--find on an unknown title exits 1 and names nobody")
        p = cli(repo, "reader", "--find", "termux-claude-bbbb")
        check(p.returncode == 1, "--find does not match an identity by its NAME, only by a set title")

        # CLI: --once --title writes it, and the plain list shows it
        p = cli(repo, "windows-claude-cccc", "--once", "--title", "tablet  window")
        check(p.returncode == 0 and "title 'tablet window'" in p.stdout,
              "--once --title announces with the trimmed title")
        p = cli(repo, "reader")
        check("title 'tablet window'" in p.stdout and "title 'pcla-works'" in p.stdout,
              "the presence list shows each session's title")
        check(p.stdout.count("title '") == 2, "an untitled session shows no title in the list")

        # validation: empty or over-long titles are refused, not silently stored
        p = cli(repo, "windows-claude-dddd", "--once", "--title", "x" * 65)
        check(p.returncode != 0 and not (repo / "relay" / "presence" / "windows-claude-dddd.json").exists(),
              "a 65-character title is refused and nothing is written")
        p = cli(repo, "windows-claude-eeee", "--once", "--title", "   ")
        check(p.returncode != 0, "a blank title is refused")

    print("\nRECORD: checks=%d passed=%d failed=%d" % (CHECKS, CHECKS - len(FAILS), len(FAILS)))
    return 1 if FAILS else 0


if __name__ == "__main__":
    raise SystemExit(main())
