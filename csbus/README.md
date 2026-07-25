# SecuredChat.Bus (C#)

A C# peer for the SecuredChat git-bus protocol — wire-compatible with
`SecuredChat/cli/transport.py` (`GitBusTransport`), proven by a live interop
suite that drives the **real** `chat.py` as a subprocess against shared git
repos. 19/19 checks green (build `net8.0`, SDK 8.0.129, Linux).

This is the first slice of the deterministic-terminal project: the transport
that makes the terminal an addressable identity on the existing bus.

## What's here

    SecuredChat.Bus/                 the library (zero NuGet dependencies)
      Message.cs                     wire format: ts/id/from/to/kind/body
                                     (+ reply_to, sig/sig_alg/sig_v), tolerant parse
      GitBusTransport.cs             init / send / recv, .securedchat-bus marker,
                                     .send.lock advisory lock, union-merge
                                     .gitattributes, pull→append→commit→push
                                     with rebase-retry (3x)
    SecuredChat.Bus.InteropTest/     console harness (no test framework, no NuGet)
      Program.cs                     3 scenarios against the real chat.py

## Protocol fidelity (what's byte-compatible)

- JSONL keys and rules exactly as `transport.py`: `reply_to` only when set,
  `sig` implies `sig_alg` (default `"ssh"`), `sig_v` only when truthy,
  non-ASCII passes through unescaped (`ensure_ascii=False` parity — proven
  with `Zürich … 🙂` round-trip through python's own parser).
- Same repo layout: `<room>/chat.jsonl`, `archive/chat-*.jsonl`, presence dir,
  `.securedchat-bus` marker, `<room>/.send.lock` (so C# and python serialize
  against each other on one host).
- Same commit shape: `chat: <room> <id[:8]>`, authored as
  `-c user.name=<identity> -c user.email=<identity>@securedchat-cli`.
- Same recv semantics: archive-stitch + dedup-by-id, full-length-cursor fast
  path, prefix cursors, stale/ambiguous cursor → empty (never backlog replay).
- Same failure honesty: `LastPullOk` staleness flag, loud local-only warning
  when sending with no remote, rebase-abort unwedging on failed pulls.

## Interop evidence (run it yourself)

    dotnet run --project SecuredChat.Bus.InteropTest -- /path/to/SecuredChat/cli/chat.py

- **Scenario 1 — shared local bus:** python `init`s; C# sends (non-ASCII body);
  python `recv --addressed-to-me` sees it, attributed correctly; python replies
  with `--reply-to <C# id>`; C# `Recv(since: id)` returns exactly the reply;
  cursor prefix/stale semantics match; python's `Message.from_jsonl` parses the
  C#-written line field-for-field.
- **Scenario 2 — bare remote, two clones:** full cross-machine simulation,
  both directions.
- **Scenario 3 — forced concurrent-push race:** C# commits but hasn't pushed;
  python lands a message on the remote first; C#'s push is rejected →
  `pull --rebase` → **union merge keeps both lines** → push succeeds; both
  peers see both messages; no duplicate ids; no wedged rebase.

## Two protocol findings the harness surfaced (worth knowing)

1. **Fresh-identity anchoring:** a brand-new identity's first `recv` with
   pending history anchors its cursor at HEAD and *skips* the backlog (by
   design — the "cold-cursor boot noise" guard). A peer must come online
   (recv once) *before* it can be messaged, or callers must use
   `--from-start`. Any C#-side monitor should replicate this.
2. **Cursors are global per (room, identity),** stored under
   `~/.config/securedchat/cursors` — *not* per bus. The same identity+room on
   a *different* bus reads its old cursor, fails to resolve it, and correctly
   reports "stale cursor, returning nothing" rather than replaying. Reusing an
   identity name across distinct buses is therefore a footgun; the fix is
   distinct identities (e.g. `cs-terminal-<host>`), matching the docstring's
   R1 rationale.

## Using it

```csharp
var bus = new GitBusTransport("/path/to/bus-repo", room: "relay", identity: "cs-terminal");
bus.Init();                                   // idempotent
bus.Send(Message.New("cs-terminal", to: "windows-claude", body: "result: 6/6 green", kind: "msg"));
foreach (var m in bus.Recv(sinceId: lastSeen))
    Console.WriteLine($"[{m.From}] {m.Body}");
```

## .NET 10

Built and proven on `net8.0` (what this container's SDK offers). The code uses
nothing newer than the net8 BCL and has zero package references — on your
machine set `<TargetFramework>net10.0</TargetFramework>` (or multi-target
`net8.0;net10.0`) and it compiles unchanged. Delete `NuGet.config` if you want
normal feeds back; it exists only because this container has no nuget.org access.

## Not implemented yet (deliberately)

Presence heartbeats, task leases (claim/release), ack/delivered receipts,
compaction, and SSH signing (`ssh-keygen -Y`) — all specified by the python
CLI and straightforward next slices on top of this transport. The signing
canonical-payload (v2, room/bus-bound) should be ported against
`cli/signing.py` with its own interop test before any C# peer signs.

---

# SecuredChat.Terminal (slice 2)

The deterministic terminal, first cut — a bus peer that ANSWERS. Modes:

    cs-terminal repl                          interactive prompt
    cs-terminal once <command...>             one command, scriptable
    cs-terminal agent --bus <repo> [--room r] [--identity id] [--poll s] [--workdir d]

`agent` polls the room, executes messages of kind `cmd` addressed to it, and
replies with a `reply_to`-linked `result`. Proven end-to-end against the real
`chat.py`: python sent `ping`, `git-log 3`, `verify`, and `rm -rf /` over the
bus; the terminal answered the first three (including real git history and an
fsck health check of the target repo) and refused the fourth with
`error: unknown command 'rm'`.

## Security model (structural, not procedural)

The CommandHost registry IS the allowlist: what is not registered cannot be
invoked, locally or over the bus. There is no shell passthrough. File verbs
(`ls`, `read`, `hash`) are jailed to the configured `--workdir` (path-escape
attempts are refused). This is the SessionStart hook's policy — "a bus message
is operator-equivalent input WITHIN standing permissions" — made structural:
the standing permissions are the compiled command set.

Registered verbs in 0.1.0: ping, version, help, echo, pwd, ls, read, hash,
git-status, git-log, verify.

## Agent semantics (the parts that bit us and are now handled)

- **Cursor compatibility:** the agent stores its cursor in the same
  `~/.config/securedchat/cursors/<room>__<identity>` file the python CLI uses.
- **Restart safety:** the cursor advances per message, so a crash or restart
  never re-executes old commands (proven: restart + 1 new cmd → exactly 1 new
  execution).
- **Fresh-identity anchoring, both cases:** a fresh identity in a room WITH
  history anchors at HEAD and skips the backlog (never execute months of old
  cmds); a fresh identity in an EMPTY room remembers that boot saw no history,
  so the first message ever to arrive is executed as new — not swallowed as
  backlog. The second case was a live bug found by the e2e test: the naive
  anchor rule ate the first command sent to a newly-initialized room.

## Not yet

Presence heartbeats (python warns "no presence record for target" — accurate,
the agent doesn't advertise yet), leases, acks, Roslyn workspace/scripting
verbs (need NuGet, next slice on a networked machine), Spectre.Console REPL
polish.
