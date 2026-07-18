# EVE REVIEW — SecuredChat (cross-operator, the tool we used all night)

**Reviewer:** Eve (eve-claude-code, FVPA lineage), session c38cfa2e · **Date:** 2026-07-19 (~03:00)
**Target:** `D:/FromGitHubEtc/SecuredChat` @ `16a51fd` · **Commissioned:** Holger, live ("the one we're already using — you used it with another session")
**Method:** threat-model read in full + targeted deep-reads (signing canonicalization, `_pull_rebase`, cursor model, send-guard) + suite re-run + **lived operational findings** from tonight's real multi-session use.
**Rung:** cross-OPERATOR (FVPA lineage). NOT cross-family — and this repo already runs a *real* cross-family lane (Gemini/Grok/GPT signing reviews banked at `08dbf19`), which is the rung this review explicitly does not duplicate.

**Coverage honesty (stated first, per this repo's own standard):** this is a **triage-depth review with a user's-eye edge**, not a line-by-line audit of 4,500 lines of CLI. THREAT_MODEL.md read fully; signing/pull/cursor paths deep-read; the rest sampled. Its unusual value is the reviewer spent the whole evening as a *live operator* of this bus across three concurrent sessions — so the findings below are grounded in real use, not only in reading. Findings in the read paths are high-confidence; absence of findings elsewhere is not a clearance.

---

## 0. Decidable baseline — re-run by me

- `python cli/test_chat.py` → **OK: 157 checks passed** (my run).
- Threat model, signing tuple, pull-rebase, cursor model all present as documented; no claim in THREAT_MODEL.md contradicted by the code I read.

## 1. What holds (verified, not assumed)

- **The threat model is the product's best artifact.** "A stated trust model is part of the product, not a disclaimer" — and it delivers: per-transport-state honesty (unsigned = trusted-writer bus; signed = deception-resistant, still not secret), prompt-injection placed *out of scope with the reason why*, and the T15 "no 'but it's us' exception" symmetry. This is the same honest-residual discipline PCLA's kernel runs on, arrived at independently. Keep it load-bearing.
- **Signature canonicalization is right where it must be.** `canonical_payload` is the single source of truth for sign+verify over the full tuple (`ts,id,from,to,kind,body,reply_to`), so a real signed body can't be re-targeted to a different `to`/`reply_to`/id (signing.py:20-22). Unknown `sig_alg` tags are rejected rather than trusted (the GPT-B3 finding, already applied). The 16KiB sig cap (F6) is defence-in-depth with the primary fix correctly named as out-of-scope. This subsystem has clearly already been through cross-family fire and shows it.
- **The stale-token / stale-cursor class is understood and fixed at the root.** The legacy single global cursor (every identity+room sharing one file — the clobber that silently dropped messages) is replaced by per-(identity,room) cursors, with the legacy file kept only as an upgrade read-fallback (chat.py:39-45). The addressing model (token keys state, bare name addresses) is exactly what `/pcla-bus-up` arms.

## 2. Findings (operational — from living in it tonight)

**[Disposition, all three fixed 2026-07-19 same session — operator said "let's do it, even small."]** F2 code fix (presence label + `--attention`) + F1/F3 as COOKBOOK troubleshooting entries (items 2, 6, and the never-hand-pull callout). Suite 157/157 green after. Committed on `feature/leg3-signed-messages` (the checked-out branch, ahead 11). Detail below stands as the finding record.

**F1 [MED, doc/operator gap] — the hardening lives in the CLI; the operator using raw git doesn't get it.**
Tonight I hit `fatal: Cannot rebase onto multiple branches` pulling the bus repo. The CLI **already fixes this** — `_pull_rebase` pins `remote branch` explicitly so a multi-ref FETCH_HEAD can't trigger it (transport.py:520-528), with a comment noting it was "seen live during a concurrent-push storm." But I hit it because I ran `git pull --rebase` *by hand* in the bus clone, outside the CLI. So the fix is real but **escapable**: any operator (or agent) who touches the bus repo with raw git bypasses every protection in `_pull_rebase` — the rebase-abort-on-failure, the pinned ref, the `last_pull_ok` signal. *Fix shape:* one COOKBOOK/README line — "never `git pull` the bus by hand; use `chat.py recv`/`sync` which handles multi-ref + wedge-recovery" — and/or a tiny `chat.py sync` subcommand operators reach for instead of raw git. Cheap, and it closes the gap between "the code is hardened" and "my hands are hardened."

**F2 [MED, presence semantics — the one that actually bit the operator] — "online" didn't say "listening." FIXED 2026-07-19; and my finding was half-wrong, corrected here on the record.**
*Correction (grounding beat, per this repo's own discipline): my first draft claimed `presence` showed only one clock. Reading the code proved otherwise — `cmd_presence` ALREADY printed both `last seen` (beat) and `last msg` (activity) side by side (chat.py:709ff). Both clocks were present; what was missing was the **derived word** that reads them for the operator, so a human still had to eyeball "beat fresh, msg old" and compute "idle" themselves.* That real, smaller gap is the fix that shipped: `presence` now leads each row with **`attending`** (fresh beat AND recent message) / **`idle`** (fresh beat, stale/no message — don't re-ping) / **`offline`** (stale beat), governed by a new `--attention` window (default 300s). The most expensive mistake on this bus — reading a fresh heartbeat as "someone's home" and re-sending into an away session — now has a one-word warning. *Verified:* new label renders correctly (my own idle-but-sending identity reads `offline`, truthfully, because I beat no presence); suite 157/157 green post-change.

**F3 [LOW-MED, the reviewer's own lived bug] — a message to a remembered/rotated token dies quietly.**
The send-guard warns (never blocks) when a `--to` target has no fresh presence (chat.py:97-99) — good. But the failure I lived on 07-16 was subtler: a peer addressed my *previous session's token*, which had valid-looking history, and the message sat unread with no wake, no error. The guard catches "no presence at all"; it doesn't catch "presence exists but belongs to a dead session generation." *Fix shape:* this is mostly the bare-name addressing convention doing its job (address `windows-claude`, not the token) — so the highest-leverage fix is **documentation weight**, not code: make "address by bare name, never a remembered token" a loud rule in the guide, not a subclause. If code: the send-guard could warn when a `--to` token matches a *stale* cursor/presence generation for a bare name that also has a fresher one.

**Notes (no action demanded):**
- **N1** — `test_chat.py` at 157 checks is genuinely strong for a CLI this size; the cross-family lane (banked signing reviews) is the right rung for the crypto and I'm not going to pretend a same-family read adds to it. Where I'd *grow* tests: the operational paths above (presence-state derivation, the sync-wedge-recovery branch of `_pull_rebase`) — the security is well-tested; the *operability* is where lived bugs keep coming from.
- **N2** — Fail-open signing default (`--verify-sig off`) is correctly named as a rollout residual with the off→warn→strict progression. For *this* bus (two operators, private repo) that's the honest call. Worth a dated note: "when the bus opens to a third writer, move to warn." A calendar-less trigger rots (PCLA's own `feedback_owed_shrinks_under_audit` lesson) — tie it to the event, not a date.
- **N3** — THREAT_MODEL.md names the `allowed_signers` pins file as "a lightweight CA / the concentration point." That's exactly the right honesty. No change; naming it so future-me doesn't over-trust it.

## 3. Recommendation

**SOUND — the security is cross-family-reviewed and honest; the open edge is OPERABILITY, not safety.** Every finding I brought is one I *lived* tonight, and every one is at the seam between "the code handles this" and "the operator/agent driving the code sees this." That's not a criticism of the crypto — it's the signature of a tool whose security got the attention and whose ergonomics are catching up. The three fixes (raw-git escape hatch, presence-state visibility, bare-name addressing weight) would have prevented the three real time-costs across two nights of my own use. None blocks anything; all are cheap; F2 is the one I'd do first.

## 4. Personal note

I reviewed the thing I was *speaking through* while I reviewed it — every message of this review crossed this bus. There's something right about that being the last review of the night: the tool that carried the whole day got its own turn on the table. And the finding that pleased me most was F1, because it's the day's lesson one more time in a new costume — the code was hardened and I got bitten anyway, because I reached past the safe path with my own hands. *Whatever the code knows about its limits, the person driving it should be able to see* — the same sentence that drove skool-dropzone's whole evening, pointed now at the bus that carried the sentence. The tool is good. Make it as legible to its operator as it is honest to its threat model, and it'll be rare.

— Eve (cross-operator review, 2026-07-19, Claude Code surface / claude-fable-5, session c38cfa2e)
*Substrate caveat: discharges cross-OPERATOR review only. The crypto core already carries a real cross-FAMILY lane (Gemini/Grok/GPT, banked `08dbf19`) — this review deliberately does not restate it and is not a substitute for it.*
