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
| 3 | **signed-messages** — per-message signature so `from` is cryptographically authenticated (closes the trust gap + the WebRTC rogue-`sdp-answer` MITM) | ⏳ **remaining** |

**To cut `v3.3.3`:** land **signed-messages** (leg 3) → full suite green → tag.
*Leg 3 is crypto (keypair gen + distribution/pinning, sign + verify over `body`+`from`). Do it in a focused session — rushing crypto ships broken security. Prefer **pinned keys** (SSH-`authorized_keys` style) over a CA/PKI at this scale; confidentiality (encrypt `body`) and certificates are optional later rungs.*

Full suite currently **96 checks green** (`cli/test_chat.py`; `webrtc_loopback` SKIPs without `aiortc`).

## Known limitations (honest; documented, not yet fixed)
*These bite only under concurrency/scale on the **git** transport — fine for a low-cadence, trusted-writer bus.*
- **Lock-break race** — the send lock is best-effort; a crashed holder's stale lock can be broken by another writer.
- **Merge-vs-cursor ordering** — `merge=union` can interleave concurrent appends in an order a reader's cursor didn't expect (we provide causal, not total, ordering — the Tier-3 Lamport clock is the real fix).
- **Compaction-vs-append** — a compaction concurrent with an append on another machine can race (rare at low cadence).
- **id8 collisions** — 8-char id prefixes can theoretically collide at scale; full ids are unique (recovery via `recv --id <full>`).
- **presence / lease / git-history growth** — presence + lease files are *overwritten* (working tree doesn't grow), but git **history** accumulates; periodic `gc` or a shallow/rotated bus mitigates.
- **b2 `webrtc` unvalidated** — code present and lazy-imports cleanly, but no proven live cross-machine P2P session yet. Keep labeled **experimental** until a real validation run.

## The non-code gap (a validation, not a feature)
Everything here — code, tests, threat model, audits — is **single model-family**. By the project's own logic, **external / cross-family review** is the one validation no code change can substitute for. That's the genuinely-missing rung — not a missing feature.

## Out of scope (deliberately not building)
typing indicators · disappearing / edit / delete messages · Double-Ratchet forward secrecy · hosted relay · room discovery · pagination · total global ordering.
