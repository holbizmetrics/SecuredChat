# Grok (Expert) — cold review of SecuredChat signing.py (2026-07-14)

*Verbatim relay by the operator from a cold chat; packet = `PASTE-SIGNING-grok.md` (byte-identical across
the 3 variants). Overall verdict: **REVISE.** Arm 2 of 3 (Gemini done; GPT 5.6 High pending).*

## Overall verdict: REVISE — core signing sound; two design points must be revised.

## Findings by section
**1. SCOPE — tight/correct.** canonical tuple binds all interpretive fields; cross-room replay BLOCKED by
`to` changing the canonical → invalid sig. **Same-message replay SUCCEEDS** (no freshness/counter/clock;
replay protection delegated to bus id-dedup, admitted deferred).
**2. TRUST STORE — clean.** Zero code path from any bus/msg field into add_pin/remove_pin/the file;
`_SAFE_NAME` + `_PUBKEY_RE` + `\n`/`\r` checks sufficient against line-injection.
**3. KEY LIFECYCLE — residual risk, by trust-model design.** Manual model invites a silent
key-substitution window: an old pinned key stays usable for impersonation until every peer runs
remove_pin; operator can be socially tricked into pinning a 2nd key. "Not a bug in the code but a direct
consequence of the chosen trust model (no certs, no signed revocations)."
**4. DOWNGRADE — the load-bearing one.** Unsigned msg with `from=Alice` (Alice pinned) → status UNSIGNED,
not BAD_SIG; under 'warn' it shows as from Alice with no crypto evidence. "The documented off→warn→strict
progression is not honest for a security property. warn is a compatibility shim that re-introduces exactly
the impersonation attack the signing was meant to stop. Only strict actually delivers per-message authorship."
**5. OTHER — nothing found.** canonicalization deterministic; name-injection blocked (list args, no shell);
tempfile `delete=False`+unlink = minor cleanup leak on crash, no usefully-exploitable TOCTOU; chmod 0o600
best-effort (silently ignored on Windows); `securedchat` namespace prevents cross-protocol raw-SSH replay.

## Revise-on (Grok's two)
1. `warn` mode = realistic low-friction unsigned impersonation → remove OR document "insecure compatibility
   mode — do not use if authorship matters." 2. Manual rotation = unavoidable impersonation window for a
   compromised old key → call out as limitation + required hygiene (prompt remove_pin, OOB rotation confirm).

## NOTE — a Gemini↔Grok DISAGREEMENT to adjudicate
Gemini F6 flagged unbounded `sig` → OOM/disk DoS (tempfile write, no size cap). Grok inspected the SAME
tempfile path and found "no security break." Real divergence on one finding; GPT arm is the tiebreak, and
DoS-vs-forgery is a severity-class question (Grok scoped to forgery/trust-store-takeover; Gemini counted
availability). Do NOT average — surface it.
