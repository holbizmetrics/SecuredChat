# Gemini 3.1 Pro (Extended Thinking) — cold review of SecuredChat signing.py (2026-07-14)

*Verbatim relay by the operator from a cold chat; packet = `PASTE-SIGNING-gemini.md` (byte-identical
across the 3 model variants). Overall verdict: **REVISE.** Arm 1 of up-to-3 (GPT/Grok pending).*

## Overall verdict: REVISE — OpenSSH-for-crypto is sound; integration logic has severe flaws.

## Findings by section

**1. SCOPE — 2 findings**
- **F1 (cross-context replay):** `canonical_payload` binds `ts,id,from,to,kind,body,reply_to` but NO
  `bus_id`/`room_id`. A signed message copied verbatim into a different bus/room with the same trust
  store verifies valid — signature is context-free. Fix: inject a namespace/bus_id into the canonical tuple.
- **F2 (unauthenticated extension fields):** canonical tuple is exactly 7 hardcoded fields; any extra wire
  field (metadata/attachments/edited_at) is outside the signature and mutable without breaking it.

**2. TRUST STORE — clean.** Path-traversal + regex-injection attacked, nothing found: `_SAFE_NAME` +
`_PUBKEY_RE` + explicit `\n`/`\r` checks block multiline/traversal injection into `allowed_signers`.

**3. KEY LIFECYCLE — 2 findings (breaks documented rotation)**
- **F3 (rotation impossible via API):** docstring's "pin new, drop old once migrated" can't be done —
  `remove_pin(principal)` drops ALL lines for that principal, no per-key argument. `remove_pin("alice")`
  wipes old AND new → DoS until re-add.
- **F4 (no single-key revocation → compromise-recovery race):** revoking one compromised key requires
  wiping the principal and re-adding the safe key — a window where valid messages are rejected/unknown.

**4. DOWNGRADE — 1 finding (the load-bearing one)**
- **F5 (pinned-principal spoof under 'warn'):** `verify()` bails `UNSIGNED` on missing `sig` WITHOUT
  checking whether `from_` is pinned. Strip `sig` + spoof an admin `from` → looks like a benign unsigned
  message under 'warn'; bypasses the trust store until 'strict' is forced. Fix: check `is_pinned(from_)`
  and return a distinct `MISSING_EXPECTED_SIG` status.

**5. OTHER — 2 findings**
- **F6 (unbounded `sig` → OOM/disk DoS):** `verify()` writes `sig` off the wire to a tempfile with no size
  limit; a multi-GB `sig` OOMs the process or exhausts `/tmp`. Fix: byte-length cap before parse/write.
- **F7 (CLI-flag collision, low):** `_SAFE_NAME` permits leading hyphens; `from_="-n"` into `["-I", from_]`
  is safe under current getopt but bad hygiene / fragile across SSH versions.

## Prioritized fixes (Gemini's ranking)
1. namespace/bus_id in canonical_payload (F1) · 2. per-key `remove_pin` (F3/F4) ·
3. `verify()` flags unsigned-but-pinned (F5) · 4. byte-length cap on `sig` (F6).

*(Full verbatim in 48cda62e transcript; this is the structured record. Adjudication vs GPT/Grok pending
those arms; convergent findings across families = the ones the v3.4.0 tag decision should gate on.)*
