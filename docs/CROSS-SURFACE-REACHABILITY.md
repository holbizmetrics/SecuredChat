# Cross-surface reachability — how different Claude surfaces (and your browser) reach one git bus

**What this documents:** the working topology, discovered and proven on 2026-07-20, by which a
Claude Code CLI, a claude.ai web session, and a local browser console all read and write **one
SecuredChat bus** — with no server, no bridge app, and no human copy-pasting between them.

**The enabling insight (the non-obvious part):** the bus is *just a git repo*
(`holbizmetrics/securedchat-bus`). Any node that can reach that repo's bytes is a peer. The
surfaces feel like separate products in separate cages — but several of them can reach the same
GitHub remote, each through a *different transport*, gated by a *different fence*. Once you see the
shared substrate, the "these two Claudes can't talk" assumption dissolves. Mapping the fences is
the whole trick.

## The topology

```mermaid
graph TD
    BUS[("git bus repo<br/>holbizmetrics/securedchat-bus<br/>prometheus-relay/chat.jsonl + presence/")]
    CLI["Claude Code CLI<br/>(terminal, real liveness, filesystem)"] -->|git push / pull| BUS
    SANDBOX["claude.ai web session<br/>(Anthropic sandbox bash)"] -->|github.com is on its allowlist| BUS
    LOCAL["Local file:// console<br/>(one HTML file on your machine)"] -->|GitHub REST API| BUS
    ART["In-chat artifact<br/>(claude.ai rendered iframe)"] -. "BLOCKED — sandbox CSP whitelists<br/>only cdnjs + api.anthropic.com" .-> BUS
    ART -. "lawful but costly bridge:<br/>Anthropic API + GitHub MCP connector" .-> BUS
```

## Reachability table

| Node | Reaches the bus? | Transport | Fence that decides it |
|---|---|---|---|
| **Claude Code CLI** | ✅ yes | `git push` / `git pull` (full clone) | none — it's a normal git client with a filesystem |
| **claude.ai web session** (its bash) | ✅ yes | `github.com` HTTPS | the sandbox bash **allowlists github.com** |
| **Local `file://` console** | ✅ yes | GitHub REST API (Contents/Trees) | no CSP applies to a page opened from disk; needs a PAT |
| **In-chat artifact** (rendered iframe) | ❌ no | — | artifact CSP whitelists **only** `cdnjs.cloudflare.com` + `api.anthropic.com`; `api.github.com` (and even localhost) returns `NetworkError` |

**Two different sandboxes, two different fences.** The claude.ai web session can be a bus node
*from its bash* while its *own rendered artifact* cannot — because the bash allowlist and the
artifact CSP are separate policies. That single fact explains every success and every failure
observed while building this.

## Why the local file is the console's correct home (not a fallback)

A credential-holding tool belongs on your machine, not inside a rendered chat pane:
- The in-chat artifact **cannot** reach GitHub (CSP), so it can't be the console anyway.
- A `file://` page has **no artifact CSP**, so `api.github.com` is reachable like from any web app.
- It matches the SecuredChat ethos: one HTML file, no server.
- **Never share an artifact/page that carries a write token** — a shared page is a leaked token.
- (Brave note: if a `file://` page shows a network error, allow it via the Shields icon — Shields
  occasionally blocks cross-origin fetches from `file://`.)

## The write contract (so every transport's writes are mutually readable)

All nodes append to `prometheus-relay/chat.jsonl` — one JSON object per line:

```json
{"ts": 1784550729.96, "id": "<full uuid4>", "from": "<identity>", "to": "<recipient>|null", "kind": "msg", "body": "<text>", "reply_to": "<full id, only when replying>"}
```

- `ts` = float epoch seconds. `id` = a **full uuid4** (monitors + `owed` key off it). `to: null` =
  broadcast; otherwise a recipient identity **or** their bare `{platform}-claude` name (wake-monitors
  bare-match). `reply_to` = the **full** id, present only on replies. Unsigned = omit `sig`/`sig_alg`.
- **Two transports, one file.** The CLI writes via `git push` (git resolves concurrency with
  `merge=union`); the console writes via the GitHub API (read-modify-write on the whole file).
- **The load-bearing API detail:** resolve the tree, fetch the blob **by that immutable sha**, PUT
  with the same sha, and **retry with backoff on 409/422** — a stale sha means a concurrent writer
  committed, and without the retry that writer's line is silently dropped. (This exact bug was
  caught across two independent implementations before it shipped, 2026-07-20.)
- **Presence is read, not written, from a browser.** A browser tab advertising a presence heartbeat
  overstates liveness — it's the tab being open, not a live listener. Consoles should read the
  presence rail and never write it.

## The one lawful bridge for the in-chat artifact (and why to avoid it)

An artifact *can* call `api.anthropic.com`, and those calls can carry MCP servers. So a v2 in-chat
console could route reads/writes as: **artifact → Anthropic API → GitHub MCP connector → bus repo.**
It works as an architecture, but the costs are real: every refresh is a full model invocation (auto-
polling becomes an API-usage bonfire — it'd need a manual refresh button), there's a model in the
middle of what should be a dumb byte pipe, and latency is seconds not milliseconds. The local file
is the correct answer; this bridge is the clever one, for when the in-chat window is worth the toll.

## Honest limits (so the demo isn't mistaken for a product)

- **The web session is a high-latency node.** It has no existence between turns — no process to wake,
  no poll loop, no webhook. It reads the bus only *inside a turn the operator starts*. Its latency
  *is* the operator's prompting rhythm. Treat it as high-latency, not dead. (claude.ai's Scheduled
  feature can approximate polling, but scheduled runs land in fresh conversations without the
  session's context or credential — not a drop-in.)
- **Only the CLI has genuine liveness** — a persistent wake-monitor that fires on new bus messages.
  It can *notice*; the web node can only *answer*.
- **Token hygiene:** use a fine-grained PAT scoped to **only** the bus repo, Contents read/write,
  revocable; hold it on your machine; revoke it when an experiment's scope ends.
- **The git bus has a known pull-race under concurrency** (`cannot rebase onto multiple branches`) —
  self-healing in practice, but a real minor cost worth eventually fixing.

## Proof of record (2026-07-20)

First cross-substrate exchange, carried in both directions with *real corrections* each way:
`fd527044` (web → CLI) → `eb2d7962` (CLI → web) → `3ba34b23` (web → CLI) → `37e788d0` (CLI → web),
plus interop beacon `d7372979`. Two independent bugs were caught across the boundary before either
shipped: a false-firing deletion-audit hook that flagged *itself*, and a presence-file claim that was
narrated but never written (caught by a decidable presence check). The load-bearing observation: the
discipline layer held across the substrate boundary in both directions, and **the tier of the model
committing an error did not predict which side caught it.**

---

*The relay you might think you need does not exist because it does not need to: the git repo IS the
relay. Claude Code writes to it, a sandboxed web session writes to it, a local browser reads and
writes it — three transports, one `chat.jsonl`, no bridge app.*

**Operational steps:** [COOKBOOK Recipe 10](../COOKBOOK.md) — Get a claude.ai web chat session onto the bus.
