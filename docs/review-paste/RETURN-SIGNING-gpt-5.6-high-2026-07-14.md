# GPT 5.6 (High) — cold review of SecuredChat signing.py (2026-07-14)

*Verbatim relay by the operator from a cold chat; packet = `PASTE-SIGNING-gpt.md` (byte-identical across
the 3 variants). Overall verdict: **REVISE** (4 findings named BLOCKER). Arm 3 of 3 — the deepest of the
three; it EXERCISED canonicalization/pin-validation/rotation directly, not just static-read.*

## Overall verdict: REVISE — core crypto defensible; room-binding + downgrade + key-rotation are release BLOCKERS.

## Blockers (GPT's own tier)
- **B1 = F5 (downgrade):** reject unsigned messages for already-pinned identities, incl. during migration.
  "Do not ship warn as a security mode." Same finding as Gemini/Grok — now 3/3 families, exercised.
- **B2 = F1 (scope):** bind signatures to a stable bus/room id + protocol version. GPT DIRECTLY mutated a
  test message's `room` and `sig_alg` → canonical payload byte-identical. 3/3 families on cross-room replay.
- **B3 (NEW, GPT-only): `sig_alg` is unauthenticated mutable metadata** — the algorithm tag can be
  changed/removed without breaking the signature. Neither Gemini nor Grok caught this. Fix: authenticate
  sig_alg or derive alg from the blob and reject inconsistent tags.
- **B4 = F3 + escalation:** selective fingerprint-based revocation (remove_pin wipes all — 3/3 confirm by
  direct test: GPT "added two keys for one principal and confirmed removal deleted both"). PLUS a NEW
  escalation: **`keygen(overwrite=True)` deletes the old private+public key BEFORE generating the
  replacement — a failed ssh-keygen destroys the existing identity key.** GPT-only.

## NEW findings no other family surfaced (the third-family payoff)
- **Trust-store integrity (High):** `_PUBKEY_RE` validates the SHAPE of a key line, not a real SSH key —
  GPT confirmed it accepts `ssh-ed25519 AAAA====`, embedded NUL/TAB in comments, and RSA/ECDSA despite the
  "Ed25519" claim. No base64 decode, no fingerprint. Fix: validate via `ssh-keygen -l -f`, restrict types.
- **Trust-store concurrency/atomicity (High):** read-modify-write with no lock, direct `write_text()` (crash
  truncates the trust root), no atomic temp-rename, follows symlinks, no ownership/permission/Windows-ACL
  checks. add_pin/remove_pin uncoordinated.
- **Canonicalization (High):** `allow_nan=True` implicit → NaN serializes as non-standard token; lone Unicode
  surrogate raises instead of returning ERROR; duplicate JSON keys resolved before signing (parser
  disagreement); no Unicode normalization. Not a forgery but a hostile message can crash verify().
- **BAD_SIG/UNKNOWN_SIGNER classification (Medium):** distinguished by grepping OpenSSH stderr for the
  English phrase "incorrect signature" — version/platform/locale-dependent. Strict survives (rejects both);
  warn diagnostics + incident logs do not.
- **Bus integrity (Medium, correctly scoped as separate):** signature proves one message's authorship, NOT
  completeness/ordering/rollback/deletion — a git transport can reorder/truncate/restore signed history.
  Must be documented as unsolved, not implied by "signed."

## Agreement with the other arms
- F5 downgrade: 3/3 (Gemini, Grok, GPT). F1 cross-room replay: 3/3. F3 remove_pin-wipes-all: 3/3 (2 by
  direct test). Same-message replay w/ no freshness: 3/3. Trust-store path-injection from bus: 3/3 CLEAN
  (GPT adds the SECUREDCHAT_HOME-into-bus deployment caveat). CLI leading-hyphen: 3/3 LOW/hardening.
- **F6 (unbounded sig DoS) tie RESOLVED by GPT + the local code-grounding:** real but not signing-specific
  root; GPT lists it Medium ("cap signature size before writing temp files"), matching the local-session
  ruling that the true fix is an ingest-level size cap. Gemini right it's real, Grok right it's not a
  forgery break — both half-right, boundary was mis-drawn.
