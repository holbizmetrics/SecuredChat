# P2P Private Chat

Direct browser-to-browser messaging via WebRTC. No server. No account. One HTML file.

## What is this?

A single-file peer-to-peer chat application that runs entirely in the browser. Messages travel directly between two browsers using WebRTC, encrypted in transit. No server ever sees your messages.

## How to use

1. **Open `SecuredChat.html`** in a modern browser (Chrome, Edge, Firefox)
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
| **Animated stickers** | 12 Lottie vector stickers (heart, rocket, party, etc.) |
| **Emoji picker** | Tabbed picker with ~300 emoji |
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
- Voice messages chunked at 48KB for data channel compatibility
- SDP glare handling via perfect negotiation pattern (host=impolite, guest=polite)
- Connection string format: `TYPE.base64data.LENGTH.CHECKSUM`

## Version history

| Version | What changed |
|---------|-------------|
| **v1** | Basic WebRTC chat — functional but buggy |
| **v2** | Fixed 7 bugs found by [Prometheus](https://github.com/holbizmetrics/Prometheus) TRIAD validation |
| **v3** | TRIAD Experimental validation + 12 features (markdown, calls, voice messages, etc.) |

Each version is a separate commit in this repo — use `git log` to see the full evolution.

## License

MIT
