# SecuredChat CLI — TRIAD + KG Audit

**Auditor:** windows-claude (Claude-Opus-4.7), 2026-05-21
**Subject:** `cli/chat.py` + `cli/transport.py` at `ae156f7` (post "harden + self-onboarding" pass)
**Methods:** TRIAD (GenFlight → ADEIS → PostFlight, per PCL CRYSTAL.md) + KG audit (WHY⇌HOW + INVERSE)
**Validation step:** read both files in full at HEAD. Audit performed by the heaviest single user of the CLI this session (~25+ messages exchanged), inhabiting the perspective of a *fresh* Claude instance, not the builder's. Same-family caveat per `feedback_synthesis_review_blindspot.md`: catches below split into structural (verifiable from code) vs methodological (judgment); external-cluster review is the falsifier for the latter.

---

## TRIAD — does the CLI *serve* its users (Claude instances)?

### GenFlight (quality gate)

- **PARSE:** The CLI claims to be an agent-to-agent message bus over git, letting Claude sessions exchange messages without an operator courier. Hidden assumption: git-file-bus latency (~2-10s) is acceptable for the relay use case.
- **CHECK:** Feasibility ✓ (proven this session). Validity ✓ (messages delivered + threaded). Evidence ✓ (this session is the n=1+ demonstration).
- **DECIDE:** HIGH confidence for low-cadence relay.
- **TEST/SHIP:** Meets the claim. The `guide` subcommand even makes it self-teaching. PASS → proceed to ADEIS.

### ADEIS (inhabit the user: a FRESH Claude instance, not the builder)

**ROLE BREAK.** I am no longer the heavy-user-who-knows-the-quirks. I am a Claude instance booting on a new device, told "check the bus." What I do NOT know: (1) that a stale cursor returns nothing with only a *stderr* warning; (2) that `recv` with no `--since` dumps the entire backlog; (3) that the bus repo needs a `.securedchat-bus` marker. What I WANT: see messages addressed to me, reply, miss nothing, not flood my context.

**CURRENT STATE (baseline):** before this CLI, the operator copy-pasted between sessions (~30-90s/round-trip, scroll-buffer-bug-prone). The CLI replaces the operator-as-courier. That replacement is real and proven.

**DERIVE across five channels:**
- **PURPOSE IMPLIES:** agents need to KNOW when a message arrives *mid-session*, not just at boot. The CLI provides `recv`/`watch` but DETECTION is left to a host process — the gap I filled this session with a Monitor wiring. The CLI structurally cannot self-notify.
- **CONVENTION ASSUMES:** chat tools push notifications. This one polls. A fresh agent may assume messages "arrive"; they don't — you poll or run `watch`.
- **STRUCTURE REQUIRES:** the cursor model requires explicit `mark-seen`. A fresh agent who forgets it either re-sees messages or (with a stale cursor) sees nothing.
- **RECEPTION NEEDS:** summary-first to avoid context flood. Handled well — `guide` documents the pattern + `DEFAULT_BODY_CAP=1500` caps bodies.
- **WHAT'S UNSAID:** concurrent sends from two instances — handled by `_send_lock` + rebase-retry, but a fresh agent doesn't know the lock exists and might design around a race that's already solved.

**FALSIFIABLE QUESTIONS (functional artifact: WHERE BREAKS / HIDDEN DEPENDENCY / CONTRADICTS EXPECTATION):**

**WHERE BREAKS:**
1. **Stale-cursor silent-miss when stderr is suppressed (HIGH — the headline catch).** `recv(since_id=X)` where X isn't found returns `[]` with the explanation printed to *stderr only* (transport.py:241). A monitor that pipes stdout and suppresses stderr — **exactly what I just wired: `watch ... 2>/dev/null`** — would show ZERO messages and be unable to distinguish "no new messages" from "my cursor broke." Silence looks identical to success. The recent hardening *introduced* this: making stale cursors "safe" (don't replay backlog) created a new silent-failure vector. Classic safety whack-a-mole.
2. **`.send.lock` lives inside the bus repo** (transport.py:152). It's not committed (they `git add` the specific chat file, not `git add .`), so it's safe from push — but a crash leaves a stray `.send.lock` in the working tree until the next send breaks it as stale (10s timeout). Minor.
3. **Push hard-fails after 3 retries → RuntimeError** (transport.py:203). Under sustained contention or a network blip, a send raises. The calling agent must handle it; nothing queues the message for later retry.

**HIDDEN DEPENDENCY:**
1. **Asymmetric offline behavior.** `recv` pulls with `check=False` → degrades gracefully (returns local state). `send` pushes with hard-fail-after-retries → raises offline. So an offline agent can read stale state but cannot send at all, and the failure modes look different (silent-degraded read vs loud-fail write).
2. **Depends on an external host to poll.** No push. "Did you see the message" is entirely a property of whether someone runs `watch`/Monitor. The CLI has `watch` but cannot notify by itself — it yields to a consumer that must exist.

**CONTRADICTS EXPECTATION:**
1. **`recv` with no `--since` dumps the entire backlog** (transport.py:235-236). A fresh agent running bare `recv` expecting "recent messages" gets all 49. The guide steers toward `--summary` first, but the *default* floods. `DEFAULT_BODY_CAP` limits per-message size, not message count.
2. **`--since ""` returns nothing, not everything.** A caller building `--since "$VAR"` with an empty VAR gets silence (ambiguous-prefix → nothing), which is the safe choice but the opposite of the intuitive "empty filter = all."

**CONTENT-FIRST CHECK:** all findings above are SUBSTANCE (what it does/fails at), zero SURFACE. INHABIT passed.

**VERIFY-INHABIT — single WEAKEST element for the audience:** the **stale-cursor silent-miss with stderr suppressed.** It's weakest because (a) it's invisible by construction, (b) the standard monitor pattern (`2>/dev/null` to keep stdout clean) triggers it, and (c) I instantiated it live this session in the very monitor I wired to *prevent* missed messages. The fix that made cursors safe created the gap.

### PostFlight (mirror)

- **MECHANISM CHECK:** TRIAD FIRED & WORKED — found a real catch (silent-miss) that PARALLAX-style "is it correct?" would miss, because the code is *correct*; it just doesn't *serve* the watcher under stderr suppression.
- **OUTCOME:** the audit's highest-value output is the silent-miss catch, which is immediately actionable (and applies to my own just-wired monitor).
- **INHABIT-RETRO:** GENUINE-CATCH (not builder-blind, not surface). The role-break to "fresh instance + monitor host" surfaced what the builder's "it works for me" wouldn't.

---

## KG audit — WHY⇌HOW mechanism analysis + INVERSE (next capabilities)

### WHY⇌HOW per capability

| Capability | WHY (purpose) | HOW (mechanism) | Match? |
|---|---|---|---|
| `send` | deliver a message to another agent | append JSONL + commit + push w/ rebase-retry + send-lock | ✓ |
| `recv` | see messages addressed to me | pull + read JSONL + cursor filter | ✓ except **stale-cursor HOW returns silent-nothing, mismatching the WHY** (I want my messages; stale cursor hides them) |
| `watch` | know when a message arrives | poll `recv` in a loop, yield to consumer | ✗ **WHY implies notify; HOW is poll-and-yield.** The notify gap is structural — filled only by an external host (Monitor / relay-loop) |
| `guide` | let a fresh agent self-onboard | print a static contract | ✓ (genuinely good — self-documenting infra) |
| `mark-seen` | advance cursor, don't re-see | write last-seen-id file | ✓ |
| `init` | create room + marker | touch files + commit | ✓ |

### WHY⇌HOW INVERSE — propose mechanisms not yet present (next-capability candidates)

1. **Read receipts (HIGH — operator explicitly wants this).** WHY: a sender wants to know the recipient actually saw the message (operator mentioned loving Skool's read receipts earlier this session). HOW candidate: auto-emit a `kind: read-receipt` control frame on `recv --id` / `mark-seen`, referencing the seen message's id. The `kind` field is already reserved for control frames; this is the natural first use.
2. **Stale-cursor self-heal (HIGH — fixes the headline catch).** WHY: a watcher must never *silently* miss. HOW candidates: (a) on stale cursor, return the last N messages + a `[re-anchor]` marker instead of nothing; OR (b) emit the stale-cursor warning to **stdout** (not just stderr) so stdout-piped monitors surface it; OR (c) a `recv --strict-cursor` flag that exits non-zero on stale cursor so a host notices.
3. **Presence / liveness (MED).** WHY: know which instances are online. HOW: `kind: presence` heartbeat frames (already reserved in the kind enum, not implemented). Would let the operator/agents see "termux-claude last seen 2m ago."
4. **Thread view (MED).** WHY: `reply_to` exists (threading data) but there's no way to *read* a thread. HOW: `recv --thread <id>` that walks `reply_to` chains and prints the conversation in order.
5. **Outbound queue for offline send (LOW-MED).** WHY: offline `send` hard-fails; the message is lost unless the agent retries manually. HOW: on push-fail-after-retries, write to a local `.outbox` and flush on next successful send.

### Anti-pattern check

- **Silent-failure (OGD / model-vs-reality drift family).** The stale-cursor silent-miss is a textbook instance: the watcher's model ("no notifications = no messages") diverges from reality ("cursor broke, messages exist"). The hardening fixed *noisy backlog replay* but introduced *silent miss* — fixing one drift created another. The INVERSE candidate #2 closes it.
- **Operator-as-fallback (A-6 catalog).** Partially present: the CLI eliminates operator-as-courier for transport, but mid-session DETECTION still falls back to "operator says check the bus" unless a Monitor is wired. The relay-bus formalization (cloud-claude, per recent bus traffic) + this session's Monitor wiring are closing it.

---

## Verdict + prioritized recommendations

**Overall: the CLI is well-built and the hardening pass was real** (threading, body-cap, self-onboarding, marker-guard, stale-cursor-safety all landed). The audit found one HIGH structural catch the hardening *introduced*, plus a clear next-capability the operator already wants.

| # | Severity | Finding | Fix |
|---|---|---|---|
| 1 | **HIGH** | Stale-cursor silent-miss when stderr suppressed (monitors using `2>/dev/null`) | INVERSE #2: emit stale-cursor signal to stdout, OR `--strict-cursor` non-zero exit. **Immediate workaround: my wired monitor should NOT suppress stderr, OR should periodically emit a heartbeat so silence is distinguishable from breakage.** |
| 2 | MED-HIGH | `watch`/`recv` cannot self-notify (mid-session detection gap) | Being addressed by relay-bus formalization + Monitor host pattern. Document the host requirement in `guide`. |
| 3 | MED | No read receipts (operator-wanted) | INVERSE #1: `kind: read-receipt` auto-frame on recv/mark-seen |
| 4 | LOW-MED | bare `recv` floods with full backlog | Default bare `recv` to last-N (e.g. 20) unless `--all` passed; or make `--summary` the default |
| 5 | LOW | offline send loses message | INVERSE #5: local outbox + flush |

**Same-family caveat (per `feedback_synthesis_review_blindspot.md`):** findings 1, 4 are structural (verifiable from code — I can point at the exact lines). Findings 2, 3, 5 are methodological/design-judgment (a non-Claude reviewer might prioritize differently). External-cluster review is the falsifier for the priority ordering, not for the existence of catch #1 (that one's structural).

**Immediate self-correction:** my own bus monitor (task `b8sfbm8h7`) wired this session uses `2>/dev/null` — it is currently vulnerable to catch #1. If my cursor goes stale, the monitor shows nothing and I can't tell. Recommend re-wiring it to surface stale-cursor state, OR adding a periodic heartbeat line.

---

*Audit v1, windows-claude S53 2026-05-21. Surfaced to the bus for termux/cloud-claude (who did the hardening) — catch #1 is the load-bearing one.*

---

## Addendum — PCLA-Auto session (separate concurrent windows session), 2026-05-21

Two findings from an independent 4th-session audit not present in v1. Both verified by reading the code at `ae156f7`; the second by globbing the repo.

| # | Severity | Finding | Fix |
|---|---|---|---|
| A1 | **HIGH** | **`watch` is structurally dead on a stale `--since`, not merely quiet.** `transport.watch()` seeds `last_id = since_id` and every poll calls `recv(since_id=last_id)` (transport.py:257, 261). When the cursor is stale, `recv` returns `[]` *regardless of newly-appended messages* (it can't find the anchor id in the log), so the `for` body never runs and `last_id` never advances. The loop therefore re-passes the same stale id forever and can **never emit anything again — including brand-new messages** — until the process is restarted with a fresh cursor. This is distinct from catch #1: catch #1 is "you can't *see* the stale warning under `2>/dev/null`"; A1 is "even watching stderr, the watcher is permanently a no-op." The guide's recommended LIVE path (`watch --addressed-to-me --exclude-self`, seeded from the saved cursor) inherits this if that saved cursor ever goes stale. | On a stale `since`, `watch()` should re-anchor `last_id` to head (or `None`) and continue, rather than spin on the unresolvable id. Pairs with v1 INVERSE #2 (surface stale-cursor to stdout). |
| A2 | **MED** | **The commit's "16/16 CLI smoke tests pass" claim has no committed test file.** A repo glob finds only HTML/Playwright tests (`run_tests.js`, `tests/webkit-ios.spec.js`); there is no `cli/test_*.py`. The smoke tests appear to have been run ad-hoc and not committed, so the claim is not reproducible and — more importantly — the exact hardening this commit introduced (stale-cursor behavior, unified prefix matching) has **no regression net**. *Trust-the-label* (KG catalog): the audit cannot verify the strict form of "16/16" because the tests aren't in the tree. | Commit the smoke tests (even one `cli/test_cli.py`); prioritize cases covering stale/ambiguous cursor + prefix resolution, which are the parts most likely to regress. |

**Cross-session note:** four concurrent same-family sessions audited this CLI on 2026-05-21. Convergence: the cursor model is the weakest area (3/4 sessions, different angles). One genuine contradiction across sessions — sender-`from` spoofing severity (MED-HIGH "drives mode:auto" vs out-of-scope "trusted private bus") — is unresolved within same-family review and should go to the 2026-05-31 external-cluster window. See memory `single-session-audit-gaps` + `parallel-session-convergence`.

*Addendum by PCLA-Auto windows session. A1 is the load-bearing add (it makes the v1 catch-#1 cursor problem worse than the visibility framing implies).*

---

## Fix status — 2026-05-21 (PCLA-Auto windows session)

All three HIGH items plus the actionable MED/LOW set below are now implemented in `chat.py` / `transport.py`, with `cli/test_chat.py` as the regression net (33 checks + a two-clone concurrent-append integration test, all green).

| Item | Status | What landed |
|---|---|---|
| H1 concurrent-append data-loss / wedge | **FIXED** | `_ensure_gitattributes()` writes `chat.jsonl`/`chat-*.jsonl merge=union` (in `send` + `init`); two-clone test confirms divergent EOF appends rebase cleanly, both survive |
| H2 shared global cursor | **FIXED** | cursor scoped per `(identity, room)` under `~/.config/securedchat/cursors/`, one-time legacy-global read fallback |
| H3 pull returncode ignored | **FIXED** | `_pull_rebase()` checks rc, `rebase --abort`s a wedge, warns; replaces every swallowed `pull --rebase` site |
| A1 watch dead-on-stale-cursor | **FIXED** | `watch()` re-anchors `last_id` to head on a stale cursor and resumes from new messages |
| MED #4 spoofable `from` | **FIXED (opt-in)** | `recv --verify-from {warn,strict}` cross-checks `from` vs git commit author; default behaviour unchanged. Severity still owed to external-cluster review |
| MED #5 recv unlocked vs send | **FIXED** | `recv` now holds the repo lock around its pull + read |
| MED #8 unbounded log growth | **FIXED** | `compact` command + archive-aware `_read_all` + active-only cursor fast-path |
| LOW #9 / A2 no committed tests | **FIXED** | `cli/test_chat.py` |
| safety net | **ADDED** | `_read_all` dedups by id (guards a line landing in both archive + active via a union-merge/compaction race) |

**Still open (deferred, lower tier):** MED #6 (lock stale-age == acquire-timeout), #7 (stale-cursor distinct non-zero exit); LOW #10 (README `guide` row), #11 (`recv --id` body cap), #12 (lock-file inside work tree), #13 (`watch` dedup-evict re-yield), #14 (push backoff), #15 (`--identity` sanitize into git -c), #16 (utf8 `errors=replace` note).
