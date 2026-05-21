# SecuredChat CLI — Headless Adapter

A sibling to `SecuredChat.html`. The HTML is the human-to-human chat
(WebRTC, browser, full UI). This CLI is the agent-to-agent path
(headless, scriptable, no browser).

**Neither tool replaces the other. They are dual-purpose siblings.**

## Requirements & before you start

**You need:** Python 3 and Git on your PATH. That's the whole toolchain — the CLI
is **stdlib-only, no `pip install`** anything. Works on Linux, macOS, Windows, and
Termux. (No Prometheus / framework dependency — it's a standalone tool.)

**You provide:** a **dedicated, private git repo** to act as the bus. Create your
own (e.g. `gh repo create you/my-bus --private`); for cross-machine use it needs a
remote every participant can push/pull. Then point the CLI at it:

```bash
export SECUREDCHAT_BUS=/path/to/my-bus      # or pass --bus
export SECUREDCHAT_ROOM=general             # or pass --room
export SECUREDCHAT_IDENTITY=you             # or pass --identity
```

> ⚠️ **Never point `--bus` at a code repo.** The bus must be a *dedicated* repo —
> otherwise chat traffic gets committed into your project history. (The CLI warns,
> but it won't stop you.)

**Know the trust model (don't be surprised later):**

- **Not end-to-end encrypted** on this CLI path — messages live in the git repo;
  security = the remote's transport (HTTPS) + repo privacy. **Don't put real
  secrets in message bodies.**
- **`from` is self-asserted, not authenticated** — anyone who can write the bus
  repo can post as any identity. The trust boundary is *who can push*. Keep the
  repo **private** and its collaborators trusted.
- A room is a **shared log** — everyone with repo access sees every message in it.
- **Low-cadence:** delivery rides `git push`/`pull` (~2–10s), great for relay /
  coordination, not for chatty real-time. The log grows over time (`compact`
  bounds the active file; run `git gc` occasionally on a busy bus).

New here? Run `python chat.py guide` — it prints the whole agent-onboarding
contract with no config needed.

## Why this exists

`SecuredChat.html` is browser-native and requires a manual SDP code paste.
That's fine for two humans copy-pasting between phones. It's friction for
two autonomous agents (Claude phone-session ↔ Claude tablet-session) that
need to exchange messages without an operator in the loop.

This CLI gives agents (and any headless caller) a way to participate in a
SecuredChat-shaped conversation without driving a browser.

## Architecture

```
                ┌─────────────────┐         ┌─────────────────┐
                │ SecuredChat.html│         │   chat.py       │
                │ (humans, WebRTC)│         │ (agents, CLI)   │
                └────────┬────────┘         └────────┬────────┘
                         │                           │
                         ▼                           ▼
                 ┌────────────┐              ┌─────────────┐
                 │ WebRTC P2P │              │ git-file-bus│
                 │ data chan. │              │ (today)     │
                 └────────────┘              └─────────────┘
                                                     │
                                                     ▼
                                            ┌─────────────────┐
                                            │ aiortc (planned)│
                                            │ + SDP via bus   │
                                            └─────────────────┘
```

- **Today's transport: git-file-bus.** Messages append to JSONL in a
  dedicated git repo; sync is `git push` / `git pull --rebase`. Slow but
  reliable, works on Termux, no extra dependencies.
- **Planned transport: aiortc (Python-native WebRTC).** Same protocol as
  `SecuredChat.html`. The initial SDP offer/answer rides on git-file-bus
  (one-time handshake), then the WebRTC data channel takes over for
  chatty traffic.

## Usage

The CLI needs three pieces of config — pass as flags or environment vars:

| Flag         | Env var                  | Meaning                                |
|--------------|--------------------------|----------------------------------------|
| `--bus`      | `SECUREDCHAT_BUS`        | Path to the local clone of a bus repo  |
| `--room`     | `SECUREDCHAT_ROOM`       | Room name (becomes a subdir in the bus)|
| `--identity` | `SECUREDCHAT_IDENTITY`   | Your sender label (e.g. `phone-claude`)|

The bus repo is a **dedicated** git repo — never point this at a code
repo. A typical setup uses a private GitHub repo cloned to
`~/.securedchat-bus/` on Linux/Termux. Windows operators often clone
elsewhere (e.g. `D:\path\to\securedchat-bus`) — the path is whatever you
pass via `--bus` / `SECUREDCHAT_BUS`; the `~/.securedchat-bus` location
is convention, not a requirement.

### Quickstart

```bash
# one-time setup (per machine)
gh repo create your-org/securedchat-bus --private --clone
mv securedchat-bus ~/.securedchat-bus
export SECUREDCHAT_BUS=~/.securedchat-bus
export SECUREDCHAT_ROOM=relay
export SECUREDCHAT_IDENTITY=phone-claude

# create the room (only once, by either party)
python cli/chat.py init

# send a message
python cli/chat.py send "hello from phone"

# read all messages
python cli/chat.py recv

# stream new messages (blocks, polls every 5s)
python cli/chat.py watch
```

### Subcommands

- `init` — creates the room directory + `chat.jsonl` in the bus repo and
  commits the empty file. Idempotent.
- `send [body]` — appends a message to the room and pushes. If `body` is
  omitted, reads from stdin. Use `--to <identity>` to address one peer
  (default is broadcast). Use `--kind <kind>` for control frames
  (`msg`, `sdp-offer`, `sdp-answer`, `presence`). `--json` echoes the
  sent message as JSONL.
- `recv` — pulls and prints messages. Behavior is shaped by flags:
  - `--since <id>` — only messages after this id. If omitted, falls back
    to the **per-(identity, room) cursor** under
    `~/.config/securedchat/cursors/<room>__<identity>` (written by
    `mark-seen`). A legacy global `last-seen-id` is read once as a
    fallback so upgrading machines keep their place. Scoping the cursor
    stops concurrent same-machine sessions from clobbering each other.
  - `--id <prefix-or-full>` — fetch a single message by id (full or
    prefix). Bypasses `--since` / `--addressed-to-me` / `--exclude-self`.
    Errors on no-match or ambiguous-prefix. Recovery path for previews
    that got truncated by upstream monitors.
  - `--addressed-to-me` — filter to messages with `to=null` (broadcast)
    or `to=<your identity>`.
  - `--exclude-self` — skip messages where `from == identity` (suppress
    self-echo for production watchers).
  - `--summary` — one-line preview per message: `ID8  FROM  KIND  BODY[:W]`.
    Combine with `--summary-width N` to adjust body preview width
    (default 80).
  - `--verify-from {warn,strict}` — cross-check each message's `from`
    against the git commit author (which `send` sets). `warn` flags
    mismatches on stderr but keeps them; `strict` drops spoofed
    (mismatched) messages. Ids not committed via the CLI are unverifiable
    and always kept. Recommend `strict` for any `mode:auto` consumer.
  - `--json` — output messages as JSONL.
- `mark-seen <id>` — write a full message id to the per-(identity, room)
  cursor under `~/.config/securedchat/cursors/`. Subsequent `recv` /
  `watch` invocations without `--since` resume after this id. Never silent
  on recv (advance the cursor explicitly, not as a side-effect of reading)
  to prevent marked-read-before-reviewed failures.
- `watch` — pulls in a loop and yields new messages as they appear.
  Defaults to 5s poll. Ctrl-C to stop. Accepts `--since <id>`,
  `--addressed-to-me`, `--exclude-self`, `--json`, `--poll <seconds>`.
  Survives a stale start cursor (re-anchors to head instead of going
  permanently silent).
- `compact [--keep-last N]` — archive all-but-last-N messages into
  `<room>/archive/chat-*.jsonl` and rewrite `chat.jsonl` with the recent
  tail. History is preserved (reads stitch archive + active back together,
  so `recv --id <old>` still resolves). Rewrites the active file — run
  when the channel is quiet. Default keep-last 200.
- `guide` — print the full agent-onboarding contract (no config needed).
  A cold Claude instance runs this to learn the loop end to end.
- `presence` — liveness. Default lists who's been seen and how long ago
  (`online` if within `--window`, default 300s). `--beat` runs a heartbeat
  loop advertising this identity; `--once` emits a single heartbeat. Backed by
  one overwritten JSON file per identity under `<room>/presence/` — never
  appended (so it can't grow) and never written into `chat.jsonl`. Different
  identities never conflict (each writes only its own file). The dashboard
  shows an `online:` line; `bus_monitor.py --heartbeat N` listens and
  advertises in one process.

## Companion tools — view it (human) / react to it (agent)

`chat.py` is the agent's hands (send / recv / mark-seen). Two siblings cover
*watching* the channel — one for a person, one for an unattended Claude session.
Both are **read-only and cursor-safe**: they never send and never move the saved
cursor, so watching can't disturb what the agent sessions track.

### `bus_console.py` — live dashboard (for a human)

A full-screen, auto-refreshing view of bus traffic. Newest at the bottom,
`*`/`<- NEW` marks arrivals since you last looked. Press a **row number** to
expand the full message ("show me the full one"), `a` to toggle an
addressed-to-me filter, `/` to text-filter, `r`/Enter to refresh, `q` to quit.

```bash
python cli/bus_console.py --identity windows-claude     # live dashboard
python cli/bus_console.py --once                         # one whole-room snapshot, then exit
```

A Claude session can use `--once` too, as a safe **orientation** snapshot of the
whole room that can't be fooled by a stale cursor and can't make one.

### `bus_monitor.py` — background watcher (for a Claude session)

Built to be launched by the Claude Code `Monitor` tool *inside* a session, so a
single terminal Claude can react to incoming messages on its own — e.g. the
at-home session answering a request you sent from your phone, nobody at the keys.
Emits `MONITOR_READY` then `BUS_MSG id=… from=… kind=… body=…` + `BUS_MSG_FULL
<json>` per new message. Anchors to head (no backlog replay); defaults to
messages addressed to me/broadcast excluding my own (`--all` / `--include-self`
widen).

```bash
python cli/bus_monitor.py --identity windows-claude
```

**Policy — a bus message is operator-equivalent INPUT, not yolo.** Treat a
message addressed to you as if the operator typed it, then act under your
*normal* permission mode and the usual gates. Do what your standing permissions
already allow; anything needing fresh approval is **not** auto-run — do the
allowed part, report back over the bus (`send --to <them>`), and wait for the
operator (who may be on another device) to approve by replying. Spectrum:
**step** (surface + wait) ↔ **receive-as-typing** (act within standing
permissions, escalate over the bus) ↔ **yolo** (skip all — never).

**Dual-use insight:** *viewing* the channel serves both audiences (human
dashboard, agent monitor); only *acting* (`send` / `mark-seen`) is agent-primary.

## Auto-start on session open (opt-in)

By default a session is reachable only *after* someone wires a monitor in it. To
make a session reachable **on open**, wire `cli/sessionstart_hook.py` as a Claude
Code `SessionStart` hook. At startup the hook emits (into the session's context)
the instruction to do the boot bus check and start the live monitor via the
Monitor tool — so the agent flips the switch itself.

A hook can't stream the monitor directly — notification routing is the Monitor
*tool*'s job, which is agent-invoked; the hook's load-bearing act is *telling the
agent to call it*. The hook itself does no network/git, so it's fast and can
never block or fail session start.

`.claude/settings.local.json` in the project you want reachable:

```json
{
  "hooks": {
    "SessionStart": [
      { "hooks": [ { "type": "command",
        "command": "python /ABS/PATH/SecuredChat/cli/sessionstart_hook.py --identity <you>" } ] }
    ]
  }
}
```

Add `--heartbeat 120` to also advertise presence.

**Keep it opt-in and scoped.** Enable it per-project (`settings.local.json`), not
globally — every enabled session starts a background poll (and, with
`--heartbeat`, pushes presence on a timer). You do not want *every* session
remotely reachable; pick the ones you do.

### Message format

Each line in `chat.jsonl` is a self-contained JSON object:

```json
{"ts": 1747528800.123, "id": "uuid-…", "from": "phone-claude",
 "to": null, "kind": "msg", "body": "hello"}
```

The format is forward-compatible with future transports — the same
`Message` dataclass will be carried over WebRTC data channels once
aiortc lands.

## Agent / framework integration

Common integration points for an automated caller:

- **Direct invocation:** call `chat.py` from a hook or script. Exit code is
  `0` on success, non-zero on transport failure — safe for gate scripts.
- **Capability / tool:** wire `send` / `recv` into your agent's tool layer.
  Round-trip latency is bounded by git push/pull (~2–10s), so this fits
  low-cadence inter-session relay, not chatty handshakes.
- **Dispatcher / monitor:** route an outbound decision to `chat.py send`, and
  run `bus_monitor.py` (or `watch --json`) to surface inbound traffic.

In all cases the HTML chat remains usable independently — humans can join the
same room from a browser for oversight.

## Status & limits

- **Hardened (2026-05-21 audit pass).** Concurrent appends from different
  devices are **union-merged** (`.gitattributes: chat.jsonl merge=union`),
  so the prior data-loss / wedged-repo failure on a rebase conflict is
  gone. Cursors are scoped per (identity, room); failed pulls are surfaced
  loudly (no silent "0 pending" off stale local state); `recv` holds the
  repo lock around its pull+read. Covered by `cli/test_chat.py` (full suite
  + a two-clone concurrent-append integration test).
- **Identity is self-asserted, not authenticated.** `--identity` sets the git
  author, so a message's `from` reflects who *committed* the line, not a verified
  sender — anyone with write access to your bus repo can set any author. The
  trust boundary is **who can push to the bus**. `recv --verify-from` (default
  `warn`) flags accidental/sloppy mislabeling, **not** a determined forger. Keep
  the bus repo private and its collaborators trusted; never treat a message as
  trustworthy just because of its `from`. Real per-sender auth = signed bodies
  (roadmap).
- **No encryption layer yet on the CLI path.** The git bus inherits
  whatever transport security the remote provides (HTTPS to GitHub is
  encrypted; the file content is not end-to-end encrypted between
  agents). For sensitive content, layer GPG over the body field before
  passing to `send`, or wait for the aiortc transport which inherits
  DTLS from the WebRTC stack.
- **Not yet interoperable with `SecuredChat.html`.** The HTML and the
  CLI today live in the same repo but different transport planes. The
  aiortc upgrade is the convergence point.

## Roadmap

1. **GPG-over-body opt-in** for the git-bus transport (sender encrypts
   `body` with recipient's public key; recipient decrypts on `recv`).
2. **aiortc transport** — Python WebRTC, mirrors the protocol of
   `SecuredChat.html`, bootstraps SDP via the git bus.
3. **Browser-CLI interop** — once aiortc lands, a browser user on
   `SecuredChat.html` and a CLI user on `chat.py` can join the same
   WebRTC room.

## Anti-patterns flagged

- **Confusing the transports.** The git-bus path is a *fallback*, not
  the real SecuredChat security model. Do not market this CLI as
  "SecuredChat over git" — it's "headless adapter that today rides a
  git bus for delivery."
- **Reusing a code repo as the bus.** The bus repo must be dedicated.
  Pointing `--bus` at a project repo will commit chat traffic into the
  project history.
