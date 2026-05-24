# SecuredChat — Threat Model

> A stated trust model is part of the product, not a disclaimer attached to it.
> This document says **exactly** what SecuredChat defends against, what it does
> not, and where the line of operator responsibility falls. If a claim isn't
> here, don't assume it.

The honest one-line summary, by transport state:

- **Without signed messages** (default until you run `keygen`/`trust`):
  a hardened, low-cadence **trusted-writer bus**. Security rests *entirely* on
  who can write the bus repo — anyone with write access can impersonate any
  identity, tamper with any body, and MITM the WebRTC handshake.
- **With signed messages** (`keygen` + peers `trust`ed + `recv --verify-sig
  strict`): **deception-resistant against repo-access attackers** — they can no
  longer impersonate, tamper, or MITM the WebRTC handshake. Trust moves from
  "who can write the repo" to "who holds the pinned private keys." Bodies remain
  plaintext-readable to anyone with repo access (not e2e-encrypted on git/file).

"Completely safe" / "unhackable" is **not** a claim this project makes. No
system's safety is provable from the inside; the only useful question is *which*
attacker, at *what* cost, with *which* residual risks named.

---

## What we defend against (with signed messages shipped + keys pinned)

- **Impersonation** of a participant — a message claiming `from: alice` verifies
  only if signed by the key pinned for `alice` (verification binds the principal
  to the claimed `from`; `from` is also inside the signed payload, so spoofing it
  breaks the signature cryptographically).
- **Body / content tampering** in transit or at rest — the signature covers the
  full content tuple (`ts, id, from, to, kind, body, reply_to`), so a stored line
  can't be silently edited, nor a real signed body re-targeted to a different
  recipient / thread / id.
- **WebRTC handshake MITM** — once the SDP is signed, a hostile bus writer can't
  post a rogue `sdp-answer` (roadmap item; the signing primitive is now in place).
- **Passive network eavesdropping on contents** — git transport runs over HTTPS
  (TLS 1.3); WebRTC data channels over DTLS. Transit is encrypted by the layer
  below us (we don't roll our own).

## What we explicitly do NOT defend against

- **Endpoint compromise.** An attacker with shell on a participant's machine
  signs with the real key. Signing authenticates whoever holds the signing
  oracle — human, agent, or attacker. Out of scope for any messaging protocol.
- **Key theft / loss.** A stolen private key forges valid messages until
  detected and revoked (`untrust`). There is no automated rotation/revocation
  protocol yet (manual `trust`/`untrust` only — see Residual risks).
- **Prompt injection of a legitimate AI participant.** A genuine, correctly
  signed message can still carry malicious instructions if the *sender* was
  injected upstream. Signing authenticates the sender, **not the sender's intent
  or compromise state.** This is the operator's responsibility (see below).
- **Confidentiality at rest on git/file transports.** Bodies are plaintext in
  the repo/dir; anyone with read access reads everything. Body encryption is a
  separate, opt-in roadmap rung. **Signed ≠ secret.**
- **Denial of service.** A bus writer can spam, delete files, or force-push to
  corrupt history. Signing stops deception, not destruction.
- **Replay** beyond id-dedup. The same signed line re-posted is deduped by `id`,
  but a monotonic-clock binding (Lamport, roadmap Tier 3) is the real fix.
- **Traffic analysis.** Sizes, timing, and who-talks-to-whom are visible to
  anyone with repo read access (or a forced TURN relay on the WebRTC path).
- **Supply-chain compromise** of the CLI, its interpreter, or dependencies
  (`aiortc`, the OS `ssh-keygen`), and **future cryptographic breaks.**

## Operator responsibilities (the line)

- **Key custody.** Protect the private keys under `~/.config/securedchat/keys/`.
  Their compromise = impersonation until you revoke.
- **First-contact / key distribution.** Pin peer keys via a channel you trust
  (`keygen` prints the public key to share out of band; peers `trust <id> <key>`).
  TOFU on a bus the attacker can already write is *not* safe on its own — the
  out-of-band step is what establishes trust; signing only enforces it.
- **Bus-write access control.** Keep the bus repo private and its writer list
  curated. Until signing is enforced (`--verify-sig strict`), this is the *only*
  security boundary.
- **Device hygiene** — signing can't help a compromised endpoint.
- **Prompt-injection mitigation at the agent layer** (see next section).

---

## Prompt injection — why it's out of scope, and what to do anyway

**Why it's structurally out of scope.** Prompt injection is not a transport
problem: the message is authentic, signed, intact — *and* poisoned. No messaging
protocol can defend against a sender's own corruption. Email has the same
boundary (a real friend's phished account sends real malware); so does every
authenticated channel. This is physics, not negligence.

**Signing makes it worse before it makes it better — name this.** Authentication
*raises* the trust a receiving agent places in a message. A compromised-but-
legitimate peer (stolen key, or an upstream-injected agent) sends a perfectly
signed message that the receiver now has *cryptographic reason to obey.* Treat a
signed message as **authenticated, not trusted**.

**Concrete mitigations the operator applies (a toolbox, not a shrug):**

1. **Treat every bus message as untrusted input at the agent layer, even when
   signed.** The signature tells you *who*, not *whether to comply*.
2. **A bus message = operator-equivalent *input*, NOT a license to skip gates.**
   The receiving session acts only within its standing permissions and escalates
   anything that would need fresh approval back over the bus — never auto-executes
   on a message's say-so. (This is the "not yolo" policy; it *is* a prompt-
   injection control — name it as one.)
3. **Minimum permissions / capability segregation.** Run the bus-connected agent
   with the least authority that lets it do its job; don't wire a broad shell
   allowlist to a session that auto-acts on inbound messages.
4. **Use `--verify-sig` (and `--verify-from`) so that even a poisoned message is
   attributable** — you can at least tell *which* peer is compromised.

**Same standard on our own use (T15).** If an agent *we* run gets injected, the
boundary still applies — we don't get a "but it's us" exception. That symmetry is
what makes this a principle rather than an excuse.

---

## Residual risks even with signing (the honest edges)

- **Fail-open during rollout.** `recv`/`watch` default to `--verify-sig off` so an
  existing all-unsigned bus doesn't break on upgrade. Signing only authenticates
  `from` once you move to `--verify-sig strict` (or set `SECUREDCHAT_VERIFY_SIG`).
  Until then, `from` is *not* authenticated. Recommended progression:
  **off → warn (once peers sign) → strict (once all keys are pinned).**
- **TOFU first-contact** is only as good as the out-of-band channel you pin over.
  On a bus an attacker can already write, a pin-first race is real — distribute
  keys through a trusted channel.
- **The pins file is a lightweight CA.** `allowed_signers` (and, if you sign it,
  the operator who curates it) is a single root of trust. Compromise of that root
  re-binds identities. This is acceptable at this scale, but it *is* the
  concentration point — name it, don't pretend it's absent.
- **Revocation window.** `untrust` removes a key locally; peers who haven't pulled
  the removal keep accepting the old key until they do. That window is the
  residual; at this cadence it's small but non-zero.
- **Same-family validation gap.** The design, audits, and this document are all
  single model-family. External / cross-family review is the one validation no
  in-family work can substitute for (see RELEASES.md "non-code gap").

---

## Roadmap rungs that shrink the residual (each a named reduction, not "safety")

- **Automated key lifecycle** — signed `key-roll` (accepted only when signed by
  the old key for voluntary rotation; out-of-band re-pin for the *compromise*
  case) and `revoke` control frames. Deferred to v3.3.4; manual `trust`/`untrust`
  is the interim story.
- **Body encryption** (sealed envelopes) — closes the at-rest plaintext / reader
  axis on git/file.
- **Lamport clock + signature-over-clock** — closes the replay axis.
- **External-cluster review** — the cross-family falsifier.
