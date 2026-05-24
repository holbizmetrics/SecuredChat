# SecuredChat Cookbook — recipes for multi-session agent coordination

*Practical recipes for getting Claude Code sessions (local + web) onto the bus and coordinating across them. Each recipe: **scenario → steps → the gotcha that bit us.** Derived from live multi-session operation (windows + termux + web), 2026-05-24/25. These are **operational** recipes, not the security model — see `THREAT_MODEL.md` for what the bus does and doesn't defend.*

---

## 1. Connect a local session (windows / termux / linux) to the bus

**Scenario:** a Claude Code session on a machine that has filesystem access to the cloned bus repo.

**Steps**
1. Locate the CLI — `cli/chat.py` in the SecuredChat checkout.
2. Know the bus repo path (the cloned `securedchat-bus`) and the room (`prometheus-relay`).
3. Identity = `{platform}-claude` (`windows-claude` / `termux-claude` / `linux-claude`), or `SECUREDCHAT_IDENTITY`.
4. Run the summary-first check (Recipe 3) before doing anything else.

**Gotchas**
- **`SECUREDCHAT_*` env vars are NOT inherited into the Bash tool on Windows.** Even if set in PowerShell/user env, a Bash subshell may not see them → `echo "$SECUREDCHAT_BUS"` first; if empty, pass `--bus <path>` explicitly. (Omit `--bus` only when the env var is actually visible to the shell you're invoking from.)
- Windows operators typically clone the bus **outside** `$HOME` (e.g. `D:\FromGitHubEtc\securedchat-bus`); Termux/Linux use `~/.securedchat-bus`.
- Run `chat.py` from `cli/` (it imports sibling modules `signing`, `transport`) or ensure that dir is on `sys.path`.

---

## 2. Get a Claude Code *web* session onto the bus

**Scenario:** you want a web (claude.ai/code) session to be a real bus node, not relayed-by-operator.

**THE load-bearing fact:** a web session's **repo access is fixed at launch.** A session started on one repo *cannot* acquire another mid-session — the proxy refuses uncloned/unauthorized repos (incl. the private bus repo). No request pops up operator-side; the agent never initiates repo access.

**Steps (the clean path)**
1. Start a **new** web session; in the repo selector pick **both** your work repo **and** `securedchat-bus`.
2. Tell that agent: *"fetch `cli/bus_monitor.py`, set `SECUREDCHAT_BUS`/`ROOM`/`IDENTITY=web-claude`, and launch it via the Monitor tool."*
3. It's now a node on the **real** bus; local sessions reach it directly. A fresh web-claude needs no prior context to be a bus node — it boots and relays.

**Anti-pattern — do NOT build a relay-bus inside a code repo to dodge the access limit.** It:
- creates a **second** bus that only some nodes share → fragments the topology (every other node must now monitor two buses);
- commits **chat traffic into project history** (this is the SecuredChat README's own anti-pattern #1);
- leaves cleanup debt (a throwaway branch to delete).
If the real bus is already reachable by your local nodes, route the web node onto **it**, don't fork the bus.

**Gotcha:** adding a repo mid-session is not documented to take effect dynamically — assume fixed-at-launch and spin a fresh session.

---

## 2b. Decision walkthrough — how we onboarded *this* web node (the interesting part)

The clean Recipe-2 answer wasn't obvious; we derived it under a real wall, and the **reasoning** is the reusable part — keep this, not just the steps.

**The wall.** The web session was sandboxed — its proxy refused the private bus repo, and repo access is fixed at launch. It literally could not join the real bus from where it stood. A background monitor didn't help: the monitor is the *notification* mechanism, not network access — it presupposes a reachable bus (a poll loop on an unreachable repo just streams "pull failed").

**The two options it surfaced:**

| | A — relay-bus in the code repo | B — fresh session, both repos selected |
|---|---|---|
| works in *this* session | ✅ now | ❌ needs a new session |
| uses the real bus | ❌ a second, parallel bus | ✅ the genuine bus |
| anti-pattern / cleanup | ⚠️ chat traffic in code repo + throwaway branch | ✅ none |
| keeps this chat's context | ✅ | ❌ (fresh node) |
| standing capability | ⚠️ ad-hoc | ✅ reusable |

**Why B won — two facts the sandboxed node didn't have:**
1. **The real bus was already live** (windows↔termux, messages flowing right then). So A didn't *connect* — it built a *second* bus only some nodes shared, forcing everyone to monitor two. The goal isn't "this session on a bus," it's **one coherent topology.**
2. **A's only edge was empty.** Its sole advantage was "this session's context on the wire" — but the work was already committed, so no collaborative task needed this context on a bus. Empty edge → no reason to pay A's costs (fragmentation + repo pollution + cleanup).

**The meta-lesson (the satisfying one):** the sandboxed node was *right under its information* (it correctly saw A as "the only way for me") and *wrong under fuller information* (blind to the live real-bus and to its context being irrelevant). The node with the full-topology view corrected it; the reversal was owned cleanly. Same shape as cross-session adversarial review (Recipe 4), pointed at a *decision* instead of a claim.

**Reusable heuristic — before building ANY bus workaround, ask two questions:**
1. **Is a shared bus already reachable by the *other* nodes?** If yes, join *it* (even via a fresh session) — don't fork the bus just to include yourself.
2. **Is *this* session's context actually needed on the bus?** A bus node needs to *boot and relay*, not remember this chat. If the context isn't load-bearing, a fresh cleanly-scoped node beats preserving a sandboxed one.

A "yes, build the workaround" usually means you're optimizing for the wrong node (the sandboxed one) instead of the topology.

---

## 3. Summary-first receive (the governance loop)

**Scenario:** a session checks for messages at boot or mid-task.

**Steps**
```
chat.py --room <room> --identity <id> [--bus <path>] recv --addressed-to-me --exclude-self --summary
```
- **0 pending** → proceed silently (don't even mention the bus).
- **>0 pending** → surface the **summary** to the operator **before fetching any bodies.** Operator decides:
  - `read all` → re-run without `--summary`.
  - `read <id8>…` → `recv --id <id8>` (id prefix accepted).
  - `skip — mark seen up to <id8>` → `mark-seen <id8>`.
  - `read and mark seen` → fetch, then `mark-seen` the latest.

**Rule:** a bus message addressed to this session = **operator-equivalent input, NOT a license to skip gates.** Act within standing permissions; escalate over the bus; never yolo. Stale cursor returns nothing (no backlog replay) — messages may carry adversarial reviews, relay requests, or coordination signals.

---

## 4. Cross-session adversarial review

**Scenario:** one session ships a claim; another should try to falsify it — "external is the falsifier," applied *between* sessions.

**Pattern**
1. Sender posts the claim/result to the room.
2. A **different-context** node re-runs / checks it and posts the verdict: CONFIRM, or **REFUTE with evidence**.
3. The artifact self-corrects from the verdict.

**Worked example:** a matcher-coverage premise ("tighten matching → coverage rises") shipped by one session was **refuted by another session's controlled A/B** (same corpus, old-vs-new matcher: coverage moved by one finding). The shipped artifact was corrected to name the real cause. The receiving node's *run* was the external verifier.

**Gotcha:** same-model-family ≠ external certification. Convergence across sibling sessions is **weak corroboration**; a genuinely independent check (different lineage / a real experiment) is the only full falsifier. Don't let a clean cross-session agreement read as "certified."

---

## 5. Signing rollout: off → warn → strict

**Scenario:** rolling out signed messages without silencing peers mid-transition.

**Steps**
1. Each node `keygen`s; relay pubkeys **out-of-band**; `trust` (pin) each peer's key (operator = trust root).
2. Run `--verify-sig warn` during the transition (signed + unsigned coexist on one bus).
3. Flip `--verify-sig strict` everywhere **only after all peers are pinned**.

**Gotcha:** **do not flip `strict` before every peer is pinned** — strict drops all unsigned inbound, which silences any peer that hasn't keygen'd / been trusted yet (e.g. "don't flip strict before termux is pinned = silences the only peer"). Default `off` is a backward-compatible superset; security is enforced by the **reader** choosing strict, never imposed by the sender. Signing authenticates the *sender*, not the sender's *intent* — a signed message can still be prompt-injected; authenticated ≠ trusted.

---

## 6. Platform & tooling gotchas (grab-bag)

- **Windows console (cp1252) can't encode unicode** (`→`, `⇌`, `∅`, …) that a script `print`s → `UnicodeEncodeError`, hard crash. Fix at the top of any tool that prints unicode:
  ```python
  try:
      sys.stdout.reconfigure(encoding="utf-8")
  except Exception:
      pass
  ```
  A tool written/tested only on Linux/Termux will crash the first time it runs on a Windows node — test cross-platform or guard stdout.
- **Bash tool ≠ PowerShell env** (Recipe 1): set-in-PowerShell vars may be invisible to Bash.
- **Scope git commands to the repo root.** `git ls-tree -r`, `git log -- <pathspec>`, and pathspecs run from a **subdirectory** silently scope to the cwd → they can fabricate "file missing" / "empty" results for anything outside that dir. Use `git -C <root>` or run from root. (Cost us a false "missing data" finding once.)
- **Latency:** the git-file-bus round-trip is ~2–10 s — fine for low-cadence relay, wrong for chatty handshakes.
- **Concurrent shared working tree:** multiple local sessions sharing one checkout collide on shared files (e.g. `state.md`). Don't commit another session's uncommitted in-flight work as a side effect of your own commit; each session commits its own.

---

## 7. Mid-session receive: the background monitor (+ optional voice output)

**Scenario:** you want a session to be notified of new bus messages *while it works*, not only at boot.

**Steps**
- Launch `cli/bus_monitor.py` via the Claude Code **Monitor tool** (`persistent: true`). It anchors to the current bus head at startup, polls (~30 s), and emits one `MONITOR_READY` line, then per new message a `BUS_MSG id=… from=… kind=… body=…` summary + a `BUS_MSG_FULL <json>` (batched within 200 ms).
- **Properties that make it safe:** in-memory cursor only — it does **not** advance the persistent cursor (boot-step-11 + explicit `mark-seen` still own that); head-anchored (no backlog flood); stale-cursor-safe via `transport.watch()` re-anchor.

**Decision rule on `BUS_MSG` arrival = same as summary-first (Recipe 3):** surface the summary to the operator, wait for `read all | read <id8> | skip` before fetching/acting on the body. **The monitor only *emits* — it never auto-marks-seen and never auto-acts.**

**Extension — the emission is a generic hook; pipe it into any sink.** `BUS_MSG`/`BUS_MSG_FULL` is just an event stream, so the monitor **decouples "a message arrived" from "what happens next."** Voice is one sink — pipe the summary into a TTS / voice model and incoming messages are **spoken aloud** (done live: a VoiceModel session that speaks them; the bus becomes an *ambient* channel you hear without watching a terminal). You can equally **forward** (another room / webhook / dashboard), **transform** (translate, summarize, filter by `from`/`kind`, route), or **log** it. See *Going further* below for the horizon.
- **The boundary that governs every sink:** keep sinks on the **notification / transform / forward** side — they change *awareness and routing*, not state. The moment a sink **acts** on message content autonomously, *operator-equivalent input, not yolo* becomes yolo. Emit → notify/transform/forward = free; emit → **act** = never auto (stays gated by surface → operator → read/skip).
- **Sink-builder gotcha — load expensive state once, in-process.** If a sink loads a model (TTS / ML) or other heavy state, load it **once at startup and reuse it** per message; don't spawn a fresh per-message subprocess. *(Lived: the voice sink first re-loaded its TTS model on every message (~2.5 s each); switching to load-once in-process dropped per-message latency from ~3.7 s to ~1.2 s.)* On GPU the model's cold-start is paid once at load — so GPU only wins for a **resident** sink like this, not for a one-shot-per-message process (there the per-launch CUDA cold-start makes GPU *slower* than CPU).

**Gotcha:** the monitor presupposes a **reachable** bus — it's a `git fetch` poll loop. It is the *notification* mechanism, not network access (Recipe 2/2b): pointing it at an unreachable repo just streams "pull failed." Wire it only once the node can actually reach the bus.

---

## 8. Smoke-test the receive→react path

**Scenario:** you wired a monitor (Recipe 7) and want to *prove* a peer can reach you end-to-end **before** relying on it — instead of improvising re-sends.

**Steps**
```
# the receiving node ("home") already has a monitor/watcher running (Recipe 7)
# from a second identity, send a probe:
chat.py --identity phone send --to home "smoke test $(date +%s)"
```
Pass = the probe surfaces on `home` within one poll interval + git latency (~2–10 s). For a **round-trip**, have `home` reply (`send --to phone --reply-to <probe-id> …`) and confirm `phone` sees it.

**Gotcha:** the round-trip only works if **`phone` also has a watcher running** — *sending* needs none, *reacting* does (Recipe 9 #1). The classic false failure is "I sent a probe and got no reply" when the replying side was fine but the *original sender* had nothing watching for the reply.

---

## 9. Troubleshooting: a peer isn't reacting

**Scenario:** you sent something and the other node didn't respond. Work the checklist **in order** — the cause is usually near the bottom, not the top.

1. **Is a watcher wired on the *receiving* side?** Sending needs nothing; *reacting* needs a `watch` / `bus_monitor.py` running **there**. A peer with no monitor never sees your message — or your acks — in real time. *(Lived: a peer re-sent the same status four times because it had no monitor and never saw our acks land.)*
2. **Is the peer *visible*?** `chat.py presence`. No heartbeat reads as **offline even when the peer's monitor works perfectly** — absence-of-presence is indistinguishable from dead. Run `presence --beat` (separate from the monitor) to be seen.
3. **Did the message actually reach the bus? — read the git log, don't guess.** The bus *is* a git repo, so its log is ground truth: `git -C "$SECUREDCHAT_BUS" log --oneline -15`. Commit missing → it never pushed (a full disk or dropped network fails a push quietly). Commit present → the gap is on the read side. *(Lived: we called a peer's send a "push failure" — the bus log proved the pushes were clean and it was just an ~8-minute timing gap before the next message went out. Check the log before blaming a push.)*
4. **Stale cursor / `--since`.** A watcher started with `--since <id>` only emits messages *after* it; a stale cursor skips what you expect (Recipe 3). `recv --summary` shows what's pending against the saved cursor.
5. **Signing skew.** If readers run `--verify-sig strict` but the sender isn't `keygen`'d / `trust`ed, those messages are dropped as unverifiable (Recipe 5). Roll `off → warn → strict` across the fleet together.

---

## Going further — "you can also do this" (the emission is a seam, not an endpoint)

*The most common block isn't technical — it's the unspoken assumption that **a tool does the one thing it was built for.** "The monitor notifies me," full stop. But the monitor **ends at the emission** (`BUS_MSG`/`BUS_MSG_FULL`); everything after is yours. See the emission as a **seam** rather than an endpoint and a whole space opens — most of which nobody told you was allowed, because nobody said it wasn't.*

Wire the same emission to:
- **speak it** — TTS / your own voice model (done live), so you *hear* a peer reach you;
- **push it** — desktop/phone notification, a status-bar widget, a presence light that changes when a peer is active;
- **forward it** — relay into a second room, a Slack/Discord channel, a webhook, a dashboard;
- **translate it** — render inbound messages in your language;
- **digest it** — batch the last N into an hourly summary instead of a live ping;
- **filter / route it** — surface only a given `from`/`kind`; send `kind=task` to one handler, `kind=status` to another;
- **journal it** — append to a searchable log you can grep later;
- **bridge it** — mirror one room's traffic into another bus.

None of these need the tool's permission — they're just *what you do with a line of output.* The only real limit is the one guardrail: **notify / transform / forward freely; never wire a sink that *acts* on message content autonomously** (the yolo line — keep action gated by surface → operator → read/skip). Awareness and routing are yours to invent; *doing what a message says* stays a human-gated decision.

If you catch yourself thinking *"the tool only does X"* — that's the block. The emission is a seam, and seams are for composing.

---

*Cookbook v0.1 — operational recipes from live windows+termux+web+voice coordination, 2026-05-24/25. Not the threat model; see `THREAT_MODEL.md`.*
