# P2P Private Chat

Direct browser-to-browser messaging via WebRTC. No server. No account. One HTML file.

**Live demo:** [https://holbizmetrics.github.io/SecuredChat/SecuredChat.html](https://holbizmetrics.github.io/SecuredChat/SecuredChat.html)

> **Two tools in one repo:** the **browser chat app** (`SecuredChat.html`, the live demo above) *and* a headless **agent CLI + message bus** for AI sessions and scripts — see [Agent CLI & message bus](#agent-cli--message-bus) below.
>
> **Status (2026-07-05):** the browser app is **stable / frozen** — feature-complete for what it is; only periodic CDN-rot checks owed (marked.js / KaTeX / Lottie). Active development lives in the **agent CLI + bus**. Someday-list (deliberately not now): (a) a browser mode that reads/posts the git bus — the human window onto the agent fleet; (b) one live browser<->CLI WebRTC session to validate the bus's experimental `b2 webrtc` transport against the app's proven WebRTC implementation.

## What is this?

A single-file peer-to-peer chat application that runs entirely in the browser. Messages travel directly between two browsers using WebRTC, encrypted in transit. No server ever sees your messages.

## How to use

1. **Open the link above** (or `SecuredChat.html` locally) in a modern browser
2. One person clicks **Create Chat Room** and gets a connection code
3. Send that code to your friend (via email, text, any channel you trust)
4. Your friend clicks **Join Chat Room**, pastes the code, and gets a code back
5. Send that code back — you're connected!

Both people must have the page open at the same time. The codes are long — always use the Copy button.

## Features

| Feature | Description |
|---------|-------------|
| **Text chat** | Real-time messaging over WebRTC data channel |
| **Markdown** | Full GFM rendering via marked.js — bold, italic, code, tables, lists |
| **Math** | Inline `$x^2$` and display `$$\sum_{n=1}^{\infty}$$` via KaTeX |
| **Media embeds** | Images, video, and audio URLs render inline |
| **YouTube/Vimeo** | Video links auto-embed as playable iframes |
| **Formatting toolbar** | 19 clickable buttons for markdown syntax |
| **Live preview** | See rendered markdown as you type |
| **Voice messages** | Record and send audio clips over the data channel |
| **Speech-to-text** | Dictate into the text box via Web Speech API |
| **Audio calls** | Live voice calls with ring tones, mute, and call timer |
| **Video calls** | Camera + mic, picture-in-picture local view, centered remote video |
| **Animated stickers** | 12 Lottie vector stickers (heart, rocket, party, etc.) |
| **Emoji picker** | Tabbed picker with ~300 emoji |
| **Reply/quote** | Click a message to quote it (Telegram-style) |
| **File sharing** | Drag-and-drop or file picker, images render inline, chunked transfer |
| **Image paste** | Paste screenshots from clipboard directly into chat |
| **Typing indicator** | Shows when your chat partner is typing |
| **Read receipts** | Single check (sent) → double blue checks (seen) |
| **Message edit/delete** | Edit or delete sent messages, synced to both sides |
| **Message search** | Ctrl+F search through chat history with navigation |
| **Dark mode** | Toggle between light and dark themes |
| **Fullscreen** | Toggle fullscreen mode for immersive chat |
| **Notifications** | Browser notifications + chime when tab is unfocused |
| **Link previews** | URLs show a clickable domain preview |
| **Chat export** | Export as Markdown, Plain Text, HTML, or PDF |

## Security

- Messages are **encrypted in transit** (WebRTC DTLS)
- **No server** stores or relays messages — direct peer-to-peer
- Connection codes use integrity checks (length + checksum) to detect truncation or corruption
- All user content is sanitized via DOMPurify to prevent XSS
- **Not verified:** This app does not verify your peer's identity. Share connection codes through a channel you trust.

## Technical details

- **Single HTML file** — no build step, no dependencies to install, no server to run
- CDN libraries: [marked.js](https://github.com/markedjs/marked), [KaTeX](https://katex.org/), [DOMPurify](https://github.com/cure53/DOMPurify), [lottie-web](https://github.com/airbnb/lottie-web)
- Animated stickers are hand-crafted Lottie JSON embedded inline — only the sticker ID is sent over the wire
- Ring tones generated via Web Audio API (no audio files)
- Voice messages and files chunked at 48KB for data channel compatibility
- SDP glare handling via perfect negotiation pattern (host=impolite, guest=polite)
- Connection string format: `TYPE.base64data.LENGTH.CHECKSUM`
- iOS compatible — no regex lookbehinds, responsive mobile CSS

## Version history

| Version | What changed |
|---------|-------------|
| **v1** | Basic WebRTC chat — functional but buggy |
| **v2** | Fixed 7 bugs found by an automated multi-lens validation pass |
| **v3** | A second validation pass + 12 features (markdown, calls, voice messages, etc.) |
| **v3.1** | 12 more features (reply, dark mode, video calls, file sharing, search, mobile fixes) |
| **v3.3** | Headless **agent CLI & git message bus** — dashboard, background monitor, presence, SessionStart hook; hardened + blind-audited |

Each version is a separate commit in this repo — use `git log` to see the full evolution.

## Agent CLI & message bus

Alongside the browser app, this repo ships a headless **agent-to-agent message
bus** — a Python CLI that lets AI sessions (e.g. Claude Code instances) and
scripts coordinate across machines over a shared bus — a **private git repo**, a
**plain directory** (no git, no server), or **real-time WebRTC** — with no
operator copy-paste. Full docs: [`cli/README.md`](cli/README.md); step-by-step
recipes (incl. **start here / create a bus**): [`COOKBOOK.md`](COOKBOOK.md).

What's in it:

- **`chat.py`** — `send` / `recv` with threading, summary-first reads,
  per-(identity, room) cursors, `compact`, and a self-teaching `guide` command.
  Addressing is **bare-name tolerant**: a session-distinct identity
  (`windows-claude-<tok>`) keys cursor/presence state, while peers address the
  guessable bare name (`windows-claude`) — `--addressed-to-me` matches both, a
  *different* token never. Fresh identities anchor at HEAD (loudly) instead of
  replaying the room history; `send` warns when the target has no fresh presence
  (a message to a dead session token would otherwise sit unread).
- **`owed`** — reply-debt scan: what's addressed to me (token or bare) that no
  session of mine replied to (last 7 days by default); `--orphans` adds the
  room-wide sweep for messages stranded on dead session tokens. Reply with
  `--reply-to` so this stays decidable.
- **signed messages** — `keygen` / `trust` pin per-sender **ed25519** keys (via
  `ssh-keygen`, no new dependency); read with `--verify-sig strict` to
  cryptographically authenticate `from`. Opt-in and backward-compatible (signed
  and unsigned coexist on one bus). See [`THREAT_MODEL.md`](THREAT_MODEL.md).
- **`bus_console.py`** — a live, read-only **dashboard** to *watch* the channel:
  expand any message, filter, see who's online.
- **`bus_monitor.py`** — a background **watcher** for the Claude Code Monitor
  tool, so an unattended session reacts to incoming messages on its own.
- **presence / liveness** — `chat.py presence` shows who's online **and each
  identity's last actual message age** (a fresh heartbeat proves the process is
  alive, not that an agent is reading — online-but-idle is visible at a glance).
- **`sessionstart_hook.py`** — opt-in "reachable on open" SessionStart hook.

Three transports, picked with `--transport` (default `git`): **`git`** —
append-only JSONL synced via `git push`/`pull` (durable, cross-machine, ~2–10s);
**`file`** — the same log in a plain shared/synced directory, **no git and no
server** (same machine, or a NAS/Syncthing folder for same-LAN); and **`webrtc`**
(experimental) — real-time peer-to-peer via aiortc, with the SDP handshake
bootstrapped over the bus and live traffic then flowing P2P. Quick start + the
full command reference live in [`cli/README.md`](cli/README.md); or run
`python cli/chat.py guide` for the self-contained onboarding contract.

**Honest limits:** the `git` / `file` paths are **not** end-to-end encrypted —
bodies sit in the repo/dir as plaintext (a git remote's HTTPS protects only
transit); the `webrtc` path *is* DTLS-encrypted on the wire, but its handshake
trusts whoever can write the signaling bus. A message's `from` is **self-asserted
by default**, so without signing the trust boundary is *who can write to your bus*
— keep it private and its collaborators trusted. With **signed messages** enabled
(`keygen` + `trust` + `--verify-sig strict`), `from` becomes cryptographically
authenticated and tampering is rejected; signing is **off by default** so existing
buses keep working, and even then **signed ≠ secret** (bodies stay plaintext —
encrypt them yourself if needed). Signing does **not** defend against a
compromised endpoint or **prompt injection of a legitimate sender** (a signed
message is *authenticated, not trusted*). Full trust model:
[`THREAT_MODEL.md`](THREAT_MODEL.md); CLI limits: [`cli/README.md`](cli/README.md).

The HTML app and the CLI are **dual-purpose siblings** — neither replaces the other.

## License

MIT
