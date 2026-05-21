# P2P Private Chat

Direct browser-to-browser messaging via WebRTC. No server. No account. One HTML file.

**Live demo:** [https://holbizmetrics.github.io/SecuredChat/SecuredChat.html](https://holbizmetrics.github.io/SecuredChat/SecuredChat.html)

> **Two tools in one repo:** the **browser chat app** (`SecuredChat.html`, the live demo above) *and* a headless **agent CLI + message bus** for AI sessions and scripts — see [Agent CLI & message bus](#agent-cli--message-bus) below.

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
scripts coordinate across machines over a shared **private git repo**
(append-only JSONL), with no operator copy-paste. Full docs: [`cli/README.md`](cli/README.md).

What's in it:

- **`chat.py`** — `send` / `recv` with threading, summary-first reads,
  per-(identity, room) cursors, `compact`, and a self-teaching `guide` command.
- **`bus_console.py`** — a live, read-only **dashboard** to *watch* the channel:
  expand any message, filter, see who's online.
- **`bus_monitor.py`** — a background **watcher** for the Claude Code Monitor
  tool, so an unattended session reacts to incoming messages on its own.
- **presence / liveness** — `chat.py presence` shows who's online.
- **`sessionstart_hook.py`** — opt-in "reachable on open" SessionStart hook.

Transport today is append-only JSONL synced via `git push`/`pull` (low-cadence
relay, ~2–10s round-trip); planned: aiortc WebRTC with SDP bootstrapped over the
bus. Quick start + the full command reference live in [`cli/README.md`](cli/README.md);
or run `python cli/chat.py guide` for the self-contained onboarding contract.

**Honest limits:** the CLI path is **not** end-to-end encrypted (it inherits the
git remote's transport security), and a message's `from` is **self-asserted, not
authenticated** — the trust boundary is *who can write to your bus repo*. Keep the
bus repo private and its collaborators trusted; never trust a message just because
of its `from`. See `cli/README.md` for the full trust model and known limitations.

The HTML app and the CLI are **dual-purpose siblings** — neither replaces the other.

## License

MIT
