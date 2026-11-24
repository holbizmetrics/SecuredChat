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

---

## Addendum 2026-11-24 — the gaps PCLA courier row 551 names (windows-claude-3c47b8a7)

**Status:** still PROPOSAL, nothing built; the operator approves before any build (row 551). This addendum closes
the design questions the row lists that the note above does not answer. Facts were checked on the Windows desktop
on 2026-11-24 and are marked as such; anything not checked says so.

### 1. The seal leaks the body if it stays outside the ciphertext (new finding)

Every message sent through `bus-send.sh` starts with `[SEAL-OPEN <length>c sha256:<12 hex>]` — the length and a hash
of the **plaintext** body, in the clear (`auto-runtime/bus-seal.py` in PCLA). If an encrypted message kept that line
outside the ciphertext, anyone with repo access could test guesses against the hash: short, predictable bodies
("ack", "done", "suite quick", a row number) would be recoverable by trying them. The length leaks too.
**Rule:** the seal goes **inside** the plaintext before encryption. Outside, the envelope may carry only facts about
the ciphertext (its length, its own hash). The acceptance test gains a check: the 12-hex prefix of the plaintext's
sha256 must NOT appear anywhere in the record.

### 2. Order: encrypt, then sign — and bind sender and recipient inside

Signing v2 (`cli/signing.py` `canonical_payload`) already covers `ts, id, from, to, kind, body, reply_to, room, bus,
sig_alg`. If `body` becomes the encrypted envelope, the existing signature covers the ciphertext unchanged. That gives
two properties worth keeping: anyone (the bus, a strict agent, a peer without the key) can check **who sent it**
before anything is decrypted, and `--verify strict` can refuse an unsigned encrypted message without touching it.
The known weakness of signing only the outside: Mallory can take Alice's ciphertext, sign it as herself and send it
to Bob, who decrypts Alice's words believing they are Mallory's. **Fix:** the plaintext inside starts with a small
header naming `from`, `to`, `room` and the message `id`; the recipient rejects a message whose inner header does not
match the signed outer fields.

### 3. Keys: a separate encryption key, certified by the pinned signing key

The note above offers (a) reuse the ssh-ed25519 signing key or (b) a separate keypair. A third option keeps the good
part of each: **(c) a separate X25519 encryption key per identity, published in a small record that the identity
signs with its already-pinned SSH key.** Effects: one trust store (you still pin only the signing key); no single key
doing two jobs; rotating the encryption key is a new signed record, visible as a change; a changed record that is
not signed by the pinned key is refused. **My recommendation is (c).**
Tooling, measured here: `age` is **not installed** on the Windows desktop (`which age` finds nothing); Python's
`cryptography` package **is** (X25519, ChaCha20-Poly1305, HKDF — everything (c) needs). Not checked: phone, tablet,
the C: box. Prefer a maintained format over a hand-rolled one; whether that is `age` (install it everywhere) or a
small documented envelope on `cryptography` is decision D2 below.

### 4. More than one recipient; broadcasts

One random content key per message, encrypted once to each recipient's key (the standard construction, and how
`age` works). The `to` field already names the recipients. **Broadcasts stay plaintext in v1**: a room-wide key
needs group membership and removal, which is a separate design. The interface must say "broadcast — readable by
anyone with repo access" rather than imply privacy.

### 5. Web peers (they write files through GitHub and run no CLI)

They have no local key store and no `chat.py`. **v1 excludes them from encrypted traffic:** they see "encrypted
message — compatible client required" and never an empty or garbled body. Prior art read: skool-dropzone Phase 2
(`skool-dropzone/PHASE2-transport.md`, PCLA 4078a5e6 / c9bfbb1f) uses WebCrypto **AES-GCM with a passphrase via
PBKDF2** — a shared-secret room model, not per-identity keys, so it does not carry over as-is; it does show WebCrypto
in the extension is workable for a later web client. Not checked: X25519 support in the browsers the web peers use.

### 6. What stays visible

`ts, id, from, to, kind, reply_to`, the signature fields, the commit history, and the **ciphertext length**. Length
is a real leak for short messages; v1 pads the plaintext to a size bucket (e.g. the next multiple of 256 bytes).
`recv`'s preview shows `[encrypted to you, N bytes]`, never plaintext, until decrypted locally; nothing decrypted is
written to cursors, logs or ACKs.

### 7. Key loss and rotation

A lost encryption private key means messages encrypted to it are **gone** — by design; say so in the interface.
Rotation keeps the retired private key in a local "old keys" list, so old messages stay readable to their owner.
A new key record must be signed by the pinned signing key; if the *signing* key itself changes, the existing
"key changed — verify out of band" path applies (`trust`), and encrypted sends to that identity are refused until
it is re-verified (Astra's step 3).

### Acceptance additions (to the three above)

4. **The row's planted negative:** a reader holding the full repo but not the recipient key recovers no plaintext
   from an encrypted row — and no seal hash of it (§1).
5. **Forwarding:** Mallory re-signs Alice's ciphertext as her own and sends it to Bob; Bob **rejects** it (§2).
6. **Unsigned key record:** an encryption-key record not signed by the pinned key is refused, and no message is
   encrypted to it (§3).

### Decisions for the operator before any build

- **D1** — key model: (a) reuse the SSH key, (b) separate unbound key, or **(c) separate key certified by the SSH key
  (recommended)**.
- **D2** — format: install `age` on every box, or a small documented envelope on Python `cryptography`.
- **D3** — v1 scope: 1:1 and small groups via the CLI only; broadcasts and web peers stay plaintext (recommended).
- **D4** — where to pilot: phone ↔ tablet as Astra proposes, or desktop ↔ phone (this box has the Python half
  already).
