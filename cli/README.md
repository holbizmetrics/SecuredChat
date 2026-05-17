# SecuredChat CLI — Headless Adapter

A sibling to `SecuredChat.html`. The HTML is the human-to-human chat
(WebRTC, browser, full UI). This CLI is the agent-to-agent path
(headless, scriptable, no browser).

**Neither tool replaces the other. They are dual-purpose siblings.**

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
`~/.securedchat-bus/`.

### Quickstart

```bash
# one-time setup (per machine)
gh repo create your-org/securedchat-bus --private --clone
mv securedchat-bus ~/.securedchat-bus
export SECUREDCHAT_BUS=~/.securedchat-bus
export SECUREDCHAT_ROOM=prometheus-relay
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
  (`msg`, `sdp-offer`, `sdp-answer`, `presence`).
- `recv` — pulls and prints all messages. `--since <id>` continues from
  after a known message. `--addressed-to-me` filters to messages with
  `to=null` or `to=<your identity>`. `--json` outputs JSONL.
- `watch` — pulls in a loop and yields new messages as they appear.
  Defaults to 5s poll. Ctrl-C to stop.

### Message format

Each line in `chat.jsonl` is a self-contained JSON object:

```json
{"ts": 1747528800.123, "id": "uuid-…", "from": "phone-claude",
 "to": null, "kind": "msg", "body": "hello"}
```

The format is forward-compatible with future transports — the same
`Message` dataclass will be carried over WebRTC data channels once
aiortc lands.

## Prometheus integration

Each Prometheus architecture has a different integration point:

- **PC (Prometheus-Crystal):** invoke `chat.py` directly from a hook or
  capability. The CLI's exit code is `0` on success, non-zero on
  transport failure — safe for gate-fire scripts.
- **PCL (Prometheus-Crystal-Lab):** wire `send` / `recv` into a
  capability under `Prometheus/capabilities/`. Round-trip latency is
  bounded by git push/pull (~2–10s on Termux), so this fits low-cadence
  inter-session relay but not chatty handshakes.
- **PCLA (Prometheus-Crystal-Lab-Auto):** integrate as a track-adjacent
  sub-loop. The R-track dispatcher can route a `path=relay` decision to
  `chat.py send`, and the monitor can `watch --json` to surface inbound
  traffic.

In all three cases, the HTML chat remains usable independently —
operators can join the same room from a browser for human oversight.

## Status & limits

- **L0 MVP.** Tested locally; round-trip via git push/pull works.
  Concurrent-sender races on the JSONL append are handled by pull-rebase-
  push retry, but pathological contention will still surface git errors.
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
