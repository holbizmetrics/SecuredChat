# Review brief — signed messages (Leg 3) · for Davide

*You now run SecuredChat in production (Nova ↔ Vulcan Lab). This brief asks you for the
one thing the project can't give itself: an **outside attempt to break the signing
design**. Your verdict gates the `v3.4.0` tag — enthusiasm doesn't count, only findings
(or a documented failure to find any). Budget: ~45–60 min.*

## What you're reviewing

Per-message **ed25519 signatures** so a message's `from` is cryptographically
authenticated and its content integrity-protected on a bus where every writer has
git push access (i.e. the transport itself proves nothing about authorship).

- Implementation: [`cli/signing.py`](../cli/signing.py) (~1 file). Backend is OpenSSH
  `ssh-keygen -Y sign/verify` — deliberately **no new dependency**.
- Trust store: an `allowed_signers` file (the SSH `authorized_keys` model): `trust`
  pins a peer's public key out-of-band; `untrust` removes it.
- Wire: optional `sig` / `sig_alg` fields on the JSONL message (old peers ignore them).
  The signature covers the **full content tuple** (not just `body`), bound to the
  claimed `from`.
- Read policies: `--verify-sig off | warn | strict` (strict = drop anything not VERIFIED).
- Threat model (what it claims and does NOT claim): [`THREAT_MODEL.md`](../THREAT_MODEL.md).

## The questions (attack these, in order of value)

*Do what fits your depth — this brief serves two reviewer profiles. The **decidable
checks below + Q4/Q5 + any usability finding from real Nova↔Vulcan use** are a full,
valuable review on their own. Q1–Q3 are code-reading questions; skip them freely —
they are also being put to independent cross-family AI reviewers in parallel.*

1. **Signature scope:** read `signing.py` — is anything an attacker cares about
   *outside* the signed tuple? (e.g. can a signed message be *replayed* into a
   different room/bus, or re-sent later, and still verify? There is deliberately no
   Lamport/replay binding yet — is that acceptable at this trust level, or a hole?)
2. **Trust-store handling:** can a malicious bus writer *modify the allowed_signers
   path or content* via anything that travels over the bus? (It must only ever change
   by local operator action.) Check `add_pin`/`remove_pin` input validation.
3. **Key lifecycle gap (known, deferred):** rotation/revocation is manual
   `trust`/`untrust`; there is no signed `key-roll` frame yet. Is the interim story
   actually safe, or does it invite silent key substitution?
4. **Downgrade:** signed and unsigned coexist (`warn` keeps unsigned). Under what
   realistic operator behavior does that coexistence quietly become "attacker sends
   unsigned as someone else and nobody notices"? Is the documented progression
   (off → warn → strict) honest about this window?
5. **The unsigned SDP handshake (known, deferred):** WebRTC offers/answers are not
   yet signed — rogue-`sdp-answer` MITM is documented as open. Given legs 1–2 of your
   own usage run on the git transport, does this matter for you *today*?

## Decidable checks (run these — "I read it" is not the review)

```bash
cd cli
python test_chat.py                      # 138 checks; test_signing needs ssh-keygen on PATH
# manual tamper check:
python chat.py --bus <your-bus> --room test --identity dav keygen
python chat.py --bus <your-bus> --room test --identity dav send "signed hello"
# edit the message body directly in the bus JSONL, then:
python chat.py --bus <your-bus> --room test --identity dav recv --verify-sig strict   # tampered msg must be DROPPED
```

## Verdict format

`PASS` (tag can be cut) / `REVISE` (specific issues, tag waits) / `REJECT` (design flaw) —
plus, for each of Q1–Q5, one line: finding or "attempted, nothing found via <what you tried>".

## Also: what changed since you got your copy (pull before next use)

`c4b5d9f` — the **stale-token black-hole pass** (directly relevant to Nova↔Vulcan):
`--addressed-to-me` now matches your **bare name** (peers can't track your session
token; a *different* token still never matches) · `send` warns when the target has no
fresh presence (dead-token guard) · new **`owed`** command = reply-debt scan,
`owed --orphans` = messages stranded on dead session tokens · fresh identities anchor
at HEAD instead of replaying the whole room · `presence` shows last-*message* age
(heartbeat-alive ≠ agent-attending). New convention: **always reply with `--reply-to`**
— thread-linkage is what makes `owed` decidable. Suite: 138 green.
