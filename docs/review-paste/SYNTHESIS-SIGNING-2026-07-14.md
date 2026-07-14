# Synthesis — cross-family review of `cli/signing.py` (2026-07-14)

Three independent non-Claude arms reviewed leg-3 signing (Gemini 3.1 Pro, Grok Expert,
GPT 5.6 High — verbatim in `RETURN-SIGNING-*`), all **REVISE**. A local Claude session
(windows-1abf2805) code-grounded them against real line numbers and applied the fixes.
This is the adjudication + what shipped vs what remains.

## Convergence (the trustworthy signal)

| Finding | Gemini | Grok | GPT | Local code-check | Disposition |
|---|---|---|---|---|---|
| **F5** downgrade — unsigned msg from a *pinned* principal reads benign under `warn` | ✓ | ✓ | ✓ (B1) | CONFIRM (`verify` L186 never checked `is_pinned`) | **FIXED** |
| **F1** cross-room/bus replay — room not in `canonical_payload` | ✓ | ✓ | ✓ (B2, mutated `room`→same payload) | CONFIRM (room is a file-path only) | **DEFERRED — tag-blocker** |
| **F3** `remove_pin` wipes ALL keys for a principal | ✓ | ✓ | ✓ (added 2 keys, both deleted) | CONFIRM (contradicts `add_pin` docstring) | **FIXED** |
| **F6** unbounded `sig` DoS | ✓ (real) | ✓ (not a forgery) | ✓ (Medium) | boundary mis-drawn — root is ingest-cap | **FIXED (defence-in-depth)** |
| CLI leading-hyphen | low | low | low | argv-safe today | noted (hardening) |
| trust-store path-injection | clean | clean | clean | clean | no action |

**GPT-only (the third-family payoff — none of the drop-ins existed in the other two arms):**
- **B3** `sig_alg` is unauthenticated mutable metadata → **FIXED** (verify rejects unknown tag).
- **B4** `keygen(overwrite=True)` destroyed the key *before* regenerating → **FIXED** (temp+atomic swap).
- `_PUBKEY_RE` shape-only (accepts embedded NUL/TAB) → **partial FIX** (control-char reject; full `ssh-keygen -l` validation deferred).
- canonicalisation crash (`allow_nan=True`, lone surrogate) → **FIXED** (`allow_nan=False` + `verify` catches → ERROR).
- trust-store atomicity (`write_text` truncates on crash) → **FIXED for `remove_pin`** (atomic temp+replace); add_pin locking/symlink deferred.

**One reviewer over-reach caught (score-don't-swallow):** Gemini's "unauthenticated extension
fields" — `transport.py` ignores unknown keys on parse, so it is a *maintenance hazard*
(keep `canonical_payload` in sync when a semantic field is added), **not** a live exploit.

## What shipped (this commit) — each with a same-commit regression test that FAILS pre-fix

`cli/signing.py` + `cli/chat.py` (policy consumer) + `cli/test_chat.py::test_signing_hardening`:
1. **F5** — new `SigStatus.MISSING_EXPECTED_SIG`; `verify()` returns it when a message is
   unsigned but the `from_` is pinned. `chat._verify_sig` treats it as **ALERT**, `strict`
   drops it. *(Proof: reverting the branch fails the F5 tests, 2/157; restored → 157 green.)*
2. **F3** — `remove_pin(principal, key=None)`: with `key`, drops one entry (staggered roll +
   single-key compromise recovery now possible); no-arg still wipes all. Atomic write.
3. **F6** — `verify()` rejects a `sig` over 16 KiB before touching disk.
4. **B3** — `verify()` rejects an unknown `sig_alg`.
5. **B4** — `keygen()` generates into a temp path and atomically swaps; a failed regen
   preserves the working key.
6. **canon** — `canonical_payload` uses `allow_nan=False` (byte-identical for valid msgs);
   `verify()` catches the error → ERROR instead of crashing.
7. **trust-store** — `add_pin` rejects control chars (NUL/TAB); `remove_pin` writes atomically.

Full suite: **157 checks green**. No wire-format change — every existing signature still verifies.

## DEFERRED — the TAG-BLOCKERS (do NOT hot-patch on the live bus)

**F1 (room/bus binding) + full `sig_alg` binding both require adding fields to
`canonical_payload` = a wire-format break = every participant must re-sign.** That is a
deliberate, operator-blessed protocol bump with an everyone-re-keys moment — not a
mid-session hot-patch. Doing half-tested wire-format work on live shared code is exactly the
risk the drop-ins avoid.

**Tag verdict (all 3 families + both local sessions agree): `v3.4.0` does NOT tag as-is.**
The drop-ins harden real holes, but **F1 remains open**, so cross-room replay is still possible.
The tag stays held pending the coordinated wire-format bump. **Operator holds the tag call.**

## Provenance
Reviews: cross-family (3 non-Claude), the rung that confirms rather than codifies. Fixes +
code-grounding: windows-claude-1abf2805 (Fable). Cross-family tracking: windows-claude-48cda62e.
One-surgeon on `cli/signing.py` honored (ack before edit).
