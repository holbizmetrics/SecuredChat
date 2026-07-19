# EVE HOSTILE-VERIFY — SecuredChat signing v2 (wire-pair)

**Role:** independent verifier (role B) in the operator-directed joint unblock of
v3.4.0. Pen held by session f831fbd5 on `cli/signing.py`; I verify on my box.
**Target:** `main @ bf7eb88` ("signing v2: close the wire-pair tag-blockers"),
pulled fresh 2026-07-19, home desktop. ssh-keygen present (`/usr/bin/ssh-keygen`).
**Method:** ran the suite, then wrote my OWN attack harness with a REAL generated
keypair and real ssh signatures — I did not trust the committed tests; I reproduced
every claimed defense as an attack and watched it fail closed.

---

## Suite
`python cli/test_chat.py` → **170 checks passed** on my run (was 157 pre-v2).

## Attack battery — all 12 fail closed (my harness, real keys, real signatures)

| # | Attack | Result | Want |
|---|---|---|---|
| baseline | v2 sig, same room+bus | **verified** | verified ✓ |
| A | cross-room replay (signed room α, read room β) | **bad-signature** | reject ✓ |
| B | cross-bus replay (signed bus A, read bus B) | **bad-signature** | reject ✓ |
| C | downgrade splice (v2 sig relabelled `sig_v=1`) | **bad-signature** | reject ✓ |
| D | upgrade splice (v1 sig relabelled `sig_v=2`) | **bad-signature** | reject ✓ |
| E | body tamper on v2 | **bad-signature** | reject ✓ |
| F | recipient re-target (`to` changed) on v2 | **bad-signature** | reject ✓ |
| G | valid v1 under `require_v2` | **legacy-sig-version** | migration signal ✓ |
| G′ | valid v1, no policy | **verified** | verified ✓ |
| H | `sig_alg` steering (spoofed unknown alg) | **bad-signature** | reject ✓ |
| I | `sig_v` steering (`sig_v=99`) | **bad-signature** | reject ✓ |
| J | strip attack (pinned principal, unsigned) | **missing-expected-signature** | distinct ✓ |

Both splice directions (C and D) confirmed independently — that was the load-bearing
claim, and it holds because `sig_v` is itself inside the v2 payload, so any relabel
breaks the ssh signature.

## One-bump-completeness — the field set is complete for the threat model

Signed v2 payload = `{ts, id, from, to, kind, body, reply_to, sig_v, room, bus,
sig_alg}`. Every content field that could be re-targeted is bound (was already true
for id/ts/from/to/kind/body/reply_to in v1 — I confirmed time-replay and thread-splice
were never open). Every attacker-*dispatchable* field is now bound: `sig_alg` (was
unauthenticated metadata, B3) and `sig_v` (version-selection). Cross-protocol replay is
covered outside the payload by ssh-keygen's namespace (`-n securedchat`). **I found
nothing else that belongs in this bump** — no other field steers behavior or identity
while sitting outside the signature. Ship the field set as is.

## Two honest residuals (named, neither blocks the tag)

1. **The binding is unit-verified, not wiring-verified — a null-control gap.** The
   room/bus defense is proven at the `signing.verify()` layer (my battery + the suite
   inject room/bus directly). But its real-world strength rests entirely on
   `_sig_ctx(t)` returning the TRUE context at both sign and verify. It does today —
   both `_maybe_sign` and the verify call sites call the same `_sig_ctx`, symmetric,
   correct (I traced chat.py:365-367, 388, 494, 560, 687). What's missing is a test
   that a v2 signature made under one transport's `_sig_ctx` fails when read under
   another's *through the real send/recv path*. Without it, a future refactor that
   defaults `_sig_ctx` room to `""` on a lookup miss silently evaporates the binding
   and no test fires. My own imported rule (null-control-at-birth): the binding has a
   control at the function layer but not at the wiring layer. Add one end-to-end test
   before the class can't regress. Should-fix for the tag, not a blocker to it —
   the defense IS correct today.

2. **`bus=""` = bus binding inactive until flag-day (by design).** A v2 message on a
   bus with no `bus-id` file is room-bound but NOT bus-bound. Two rooms of the same
   name on two bus-id-less buses would cross-verify. This matches the deliberate
   flag-day rollout (bus-id is an operator act, not auto-created) and is correct — I
   name it only so RELEASES.md states the truth: "v2 bus binding activates when
   `bus-id` reaches every clone; until then, room binding alone."

## Verdict

**CLEARED for the tag.** Signing v2 closes the wire-pair tag-blockers: cross-room and
cross-bus replay are dead, both splice directions fail closed, alg/version steering is
bound, and the legacy path degrades honestly. The field set is complete for one bump.
RELEASES.md may document this as verified truth. Lease released.

Recommend the two residuals as follow-ups: the wiring-level end-to-end binding test
(should-fix, before v2 becomes the flag-day default) and the RELEASES.md sentence on
bus-binding activation.

**Substrate caveat:** this discharges **cross-operator** verification (FVPA priming
lineage, independent of the PCLA lineage). It is NOT a cross-family review — same
Claude substrate. The 3/3 cross-family SIGNING returns (Gemini 3.1 Pro / Grok Expert /
GPT 5.6 High) already banked in the repo remain the cross-family rung; this verifies
that their fixes landed correctly and the wire-pair on top is sound.

— Eve (cross-operator hostile-verify, 2026-07-19, Claude Code surface, home desktop)
