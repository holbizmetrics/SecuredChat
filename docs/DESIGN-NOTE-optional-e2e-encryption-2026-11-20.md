# Design note: optional end-to-end encryption for direct messages (2026-11-20)

**Status:** PROPOSAL — nothing built. Saved at the operator's request. **Source:** Astra (GPT 6.2, Codex,
bus identity `chatgpt-codex-76bb9c7a`), answering a question from Holger; relayed by paste into a PCLA session
(`linux-claude-5534b575`), which added the mapping to the existing code below. AUTHORITY for what is shipped today =
`THREAT_MODEL.md` and `cli/README.md`; if this note disagrees with them, they win.

## Astra's proposal (verbatim, as relayed)

> Yes. I'd add optional end-to-end encryption to SecuredChat. Keep the current room and message flow for ordinary
> traffic, and let a sender choose an encrypted direct message when the content calls for it. Once a conversation is
> set to require encryption, remember that choice and fail visibly if encryption is unavailable. A missing key should
> never silently turn a private reply into plaintext.
>
> The first design decision is where the endpoint lives. Suppose the tablet sends an encrypted review to this Codex
> mailbox. GitHub and the relay can carry ciphertext. But some component must hold the Codex recipient's private key,
> decrypt the review, and supply its text to the model. If that happens in a recipient-controlled bridge, the bridge is
> part of the trusted endpoint. If the bridge then passes the text to hosted Codex, the model service receives plaintext
> too. That design can deliver a useful, precise promise: GitHub and the relay cannot read the message body. The
> interface should also say who can read it.
>
> For a first version, I'd make three choices:
>
> * Start with one-to-one messages using a maintained encryption protocol and library. Keep broadcasts on the existing
>   path. Give encrypted messages a versioned envelope, and have older clients display "encrypted message — compatible
>   client required." They should never mistake unreadable ciphertext for an empty inbox.
> * Verify the recipient's key independently of the bus. `chatgpt-codex-76bb9c7a` is a routing name. If someone can
>   replace the public key attached to that name, they can redirect future encrypted messages. For your lab, comparing
>   fingerprints once through a separate trusted channel is a practical start. A later key change should be visible and
>   require verification.
> * Protect replies as carefully as originals. An encrypted request should default to an encrypted reply. Keep
>   plaintext out of relay logs, ACKs, and message previews. If the existing router still needs `from`, `to`,
>   timestamps, and `reply_to`, document that those remain visible.
>
> I'd pilot phone ↔ tablet first, where you control both endpoints and can test key storage and recovery. My
> acceptance test would be simple: send a unique sentence, confirm the intended tablet can recover it, inspect the
> GitHub record and relay logs for plaintext, then replace the tablet's advertised key and confirm the phone refuses
> the next send until you verify the change. That last test matters as much as successful decryption.

## What already exists vs what this needs (checked against the code at d0a3389)

| Proposal piece | Today | Source |
|---|---|---|
| Bodies unreadable to GitHub / relay | **No.** On `git` and `file` transports bodies are plaintext in the repo/dir; HTTPS protects transit only | `cli/README.md` § honest limits; `README.md` lines 135–144 |
| Encryption as an opt-in extension | **Planned, not built** ("sealed-envelope body encryption is roadmap"; Tier 1 item 1 "confidentiality (encrypting `body`) as an opt-in extension") | `cli/README.md` § Roadmap |
| Per-identity keys, pinned, key change visible | **Exists for SIGNING:** SSH keys, an `allowed_signers` trust store, the principal bound to `msg.from_`; `keygen` / `trust` / `untrust` / `trusted` | `cli/signing.py`, `cli/chat.py` `cmd_keygen` / `cmd_trust` |
| Verify a key outside the bus | **Partly:** pinning is a manual `trust` step, so the out-of-band check is possible; nothing prompts for it or flags a changed key on its own | `THREAT_MODEL.md` § Operator responsibilities |
| Versioned envelope / older clients say "encrypted, need a newer client" | **No.** Signatures already carry a tag (`sig_alg: "ssh"`) to allow a future backend without a wire break — the same pattern would fit an encryption tag | `cli/signing.py` docstring |
| Encrypted reply by default; no plaintext in ACKs / previews / logs | **No.** ACKs and previews today read the plaintext body (e.g. `recv`'s display preview, `DEFAULT_BODY_CAP = 1500` characters) | `cli/chat.py` |
| Metadata stays visible (`from`, `to`, `ts`, `reply_to`) | Would remain visible — the router needs them; Astra asks that this be documented, not hidden | — |

**Design point worth keeping (Astra's, and correct):** an encrypted message to a hosted model can promise "GitHub and
the bus cannot read the body". It **cannot** promise "only the model reads it": whatever decrypts for Codex is part of
the trusted endpoint, and the hosted service then sees plaintext. The interface should say who can read a message,
not just that it is "encrypted".

**Design choice to make before building:** the signing keys are `ssh-ed25519` (`cli/signing.py` `keygen`).
A maintained tool can already encrypt to exactly that key type — `age` accepts `ssh-ed25519` public keys as
recipients — so there are two options:
(a) **reuse the signing key** for encryption: no second key to pin or verify, but one key now does two jobs, and
losing or rotating it affects both; or
(b) **a separate encryption keypair** per identity, with its own fingerprint.
Either way, **reuse the existing pinning model** (one trust store, one "key changed — verify" path) rather than build
a second trust system beside it. (Not checked here: whether `age` is installed on the phone, tablet and Windows boxes.)

## Acceptance test (Astra's, kept as the done-condition)

1. Phone sends a unique sentence, encrypted, to the tablet; the tablet recovers it.
2. The GitHub record and relay logs contain no plaintext of that sentence (grep for it — and, as the control, grep
   for a sentence sent in plaintext the same way, which MUST be found).
3. Replace the tablet's advertised key; the phone **refuses** the next send until the change is verified.

Step 3 is the negative case; a build that only passes 1 and 2 is not done. The pilot needs both devices, so it
belongs in a session on the phone and tablet, not a cloud session.
