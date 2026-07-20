# SecuredChat — Releases & Status

> Self-documenting handoff. **Latest tag: `v3.3.2`.** `v3.3.3` is in flight.

## Release discipline
Releases are **tag-gated**: batch coherent work before tagging · **merge ≠ release** · don't cut a tag just to cut one (no docs-only / single-tweak tags).

## v3.3.3 — in flight (the *acknowledged · correlated · coordinated* Tier-1 layer + transports)

**Scope (locked):** the Tier-1 trio + the already-merged transports. Tier-2/3 deferred.

| Leg | Item | Status |
|---|---|---|
| transports | `file` (gitless shared dir) + `webrtc` (experimental P2P) | ✅ merged (`757ccb3`, `dffa789`) |
| 1 | **task-lease** — `claim` / `release` / `leases`; conflict-free per-(work-id, identity) leases, first-claimer-wins, TTL expiry | ✅ shipped `d8fb267` |
| 2 | **delivery-ack** — `ack` / `delivered` / `recv --ack`; `kind=ack` receipts (`reply_to`=acked id) | ✅ shipped `fbd106b` |
| 3 | **signed-messages** — per-message ed25519 signature (via `ssh-keygen -Y`, no new dep) so `from` is cryptographically authenticated + body is integrity-protected | ✅ **shipped** (branch `feature/leg3-signed-messages`) |

**Leg 3 (shipped) — what's in:** `keygen` / `trust` / `untrust` / `trusted`, auto-sign on send/ack/connect, `recv`/`watch --verify-sig {off,warn,strict}` (+ env `SECUREDCHAT_VERIFY_SIG`). Backend is OpenSSH `ssh-keygen -Y sign/verify`; trust store is an `allowed_signers` file (the SSH-`authorized_keys` model). Signature covers the full content tuple (not just `body`), bound to the claimed `from`. New module `cli/signing.py`; wire gains optional `sig`/`sig_alg` (backward-compatible — old peers ignore them). See `THREAT_MODEL.md` for exactly what this does and doesn't buy.

**To cut `v3.3.3`:** signing is in + full suite green → **but do NOT tag yet.** The one rung no in-family work substitutes — **external / cross-family review of the signed-messages design** (the 2026-05-31 window the audit doc names) — gates the *tag*, not the merge. Tag after that review.

**Update 2026-07-02 — reviewer identified + more shipped-unreleased scope:**
- **Reviewer candidate: Davide (Holodeck23)** — now a real SecuredChat *user* (his Nova and Vulcan Lab architectures, both Claude Code, talk to each other over the bus): an external human with operational stakes. His adoption is an adoption signal, NOT yet the review — the review brief (`docs/REVIEW-BRIEF-signed-messages.md`) turns it into one. A verdict there unblocks this tag.
- **Shipped on main since (`c4b5d9f`) — the stale-token black-hole pass, part of the next tag's scope:** bare-name matching in recv/watch/ack `--addressed-to-me` (a bare-addressed reply can no longer be silently dropped by the filter), send-side no-fresh-presence warning + narrative-addressing lint, new `owed` command (reply-debt, `--days`, `--orphans` dead-token sweep with likely-yours tagging), fresh identities head-anchor loudly (`--from-start` replays), presence shows last-message age. 17 new checks; suite now **138 green**. Grounded in five recorded live incidents (worst: a reply addressed to a rotated session token sat unread 5h while a wrong conclusion was banked on its absence).
- Since `c4b5d9f` adds user-facing features beyond the locked v3.3.3 scope, the post-review tag should be **`v3.4.0`** (scope = locked Tier-1 trio + transports + black-hole pass), superseding the v3.3.3 label above.
- **Field-report fixes (same day, peer session testing `owed` in production):** `send` now resolves `--reply-to` PREFIXES to full ids on the wire (a prefix reply made a real answer look unanswered); `owed`/`--orphans` clear by prefix so historical threaded replies count; the send-side liveness guard counts actual message recency alongside the heartbeat (a peer that messages without a beat was warned as "last seen 115h ago" while it had answered 30 min earlier). Suite: **141 green**. The bus's first external field test caught all three within hours of shipping — the feedback loop the tool exists for, pointed at itself.

**Update 2026-07-19 — wire-pair tag-blockers CLOSED + verified; v3.4.0 CUT:**
- `canonical_payload` **v2** binds `room` + `bus` + `sig_alg` + `sig_v` itself (splice-proof in
  both directions: a v2 message re-labelled v1, or v1 re-labelled v2, breaks the signature).
  Verify binds to the CALLER's room/bus context, never message fields. Suite **170 green**.
- **Independently hostile-verified cross-machine** (`EVE-VERIFY-signing-v2-2026-07-19.md`,
  `d1970d3`): 12/12 attack battery fail-closed with a real keypair (cross-room, cross-bus,
  both splices, tamper/re-target, alg-steering, unknown version, strip, policy matrix);
  one-bump-completeness review found no further field that steers behavior outside the signature.
- **Migration mechanics:** sign stays v1-default until the `SECUREDCHAT_SIG_V2` flag-day;
  after it, `SECUREDCHAT_REQUIRE_SIG_V2` classifies valid v1 signatures as `LEGACY_SIG`
  (warn flags, strict drops). **Bus binding is ROOM-ONLY until a `bus-id` file reaches every
  clone** — creating it is a deliberate operator flag-day act, never a send side effect.
- Named should-fix **before flag-day flips the default** (not tag-gating): the binding is
  unit-verified at `signing.verify()`; add one end-to-end wiring test through send/recv so a
  refactor defaulting room to "" cannot silently evaporate the binding unnoticed.
- The external-review rung the old "do NOT tag yet" text gated on was discharged by the
  2026-07-14 cross-family review (3 non-Claude arms + adjudication). The Davide human review
  brief stays open as an additional rung, no longer tag-gating.

**Deferred to `v3.3.4` (named, not forgotten):**
- **Automated key lifecycle** — signed `key-roll` frame (accepted only when signed by the *old* key; the *compromise* case needs out-of-band re-pin, a distinct path) and `revoke` frame. Interim story is manual `trust`/`untrust`, which already gives a working rotation+revocation.
- **Sign the WebRTC SDP handshake** to close the rogue-`sdp-answer` MITM — the signing primitive is now in place; applying it to `sdp-offer`/`sdp-answer` frames is the remaining wiring.
- Optional **body encryption** (sealed envelope) and **Lamport-clock replay** binding remain Tier-2/3 as before.

Full suite currently **138 checks green** (`cli/test_chat.py`; `test_signing` SKIPs without `ssh-keygen`; `webrtc_loopback` SKIPs without `aiortc`).

## Known limitations (honest; documented, not yet fixed)
*These bite only under concurrency/scale on the **git** transport — fine for a low-cadence, trusted-writer bus.*
- **Lock-break race** — the send lock is best-effort; a crashed holder's stale lock can be broken by another writer.
- **Merge-vs-cursor ordering** — `merge=union` can interleave concurrent appends in an order a reader's cursor didn't expect (we provide causal, not total, ordering — the Tier-3 Lamport clock is the real fix).
- **Compaction-vs-append** — a compaction concurrent with an append on another machine can race (rare at low cadence).
- **id8 collisions** — 8-char id prefixes can theoretically collide at scale; full ids are unique (recovery via `recv --id <full>`).
- **presence / lease / git-history growth** — presence + lease files are *overwritten* (working tree doesn't grow), but git **history** accumulates; periodic `gc` or a shallow/rotated bus mitigates.
- **b2 `webrtc` unvalidated** — code present and lazy-imports cleanly, but no proven live cross-machine P2P session yet. Keep labeled **experimental** until a real validation run.
- **Send-time pull-race** — `git pull --rebase` in the send path can fail with `fatal: Cannot rebase onto multiple branches` when a fetch brings in multiple updated refs mid-send; observed ~5–6× in a burst of concurrent multi-node writes (2026-07-20). It **self-heals** (the send's own commit+push still lands), so it's noise-with-a-warning, not data loss — but it is a real defect in the pull step, not just cosmetic. Fix candidate: pin the rebase to the tracked upstream branch (`@{u}`), or fetch-then-fast-forward the single ref instead of a bare `pull --rebase`.

## The non-code gap (a validation, not a feature)
Everything here — code, tests, threat model, audits — is **single model-family**. By the project's own logic, **external / cross-family review** is the one validation no code change can substitute for. That's the genuinely-missing rung — not a missing feature.

## Out of scope (deliberately not building)
typing indicators · disappearing / edit / delete messages · Double-Ratchet forward secrecy · hosted relay · room discovery · pagination · total global ordering.
