"""Per-message signing for SecuredChat (leg 3 of v3.3.3).

Backend: OpenSSH's `ssh-keygen -Y sign` / `-Y verify` (needs OpenSSH >= 8.2,
which ships with git). No third-party dependency, and the trust store is an
`allowed_signers` file — literally the SSH-`authorized_keys` model RELEASES.md
committed to. The signature is an armored SSH-SIGNATURE blob carried in the
JSONL line as `sig`, tagged `sig_alg: "ssh"` so a future backend can be added
without a wire-format break.

Trust model (what this does and does NOT do):
  - VERIFIED means: the message was signed by the key PINNED for the claimed
    `from` (the `-I` principal is bound to `msg.from_`, never a self-asserted
    field). It moves the trust boundary from "who can write the bus repo" to
    "who holds the pinned private keys".
  - It does NOT provide confidentiality (bodies stay plaintext), replay
    protection beyond id-dedup, or any defense once a key/endpoint is
    compromised. See THREAT_MODEL.md.

The single load-bearing implementation detail is canonicalisation: sign and
verify MUST feed byte-identical input to ssh-keygen. `canonical_payload` is the
one source of truth for both; it covers the full content tuple (not just body)
so a real signed body can't be re-targeted to a different `to`/`reply_to`/id.
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import tempfile
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

# Namespace binds a signature to THIS protocol, so an ssh signature made for
# another purpose (git, email) can't be cross-protocol replayed onto the bus.
NAMESPACE = "securedchat"
SIG_ALG = "ssh"  # wire tag; the armored blob self-describes the key type

# Same charset as transport._SAFE_NAME — principals must be safe for the
# allowed_signers file (no spaces/newlines that could inject extra entries).
_SAFE_NAME = re.compile(r"^[A-Za-z0-9._-]+$")

# A well-formed single-line SSH public key. Rejects anything with an embedded
# newline (allowed_signers line injection) or an unknown key type.
_PUBKEY_RE = re.compile(
    r"^(ssh-ed25519|ssh-rsa|ecdsa-sha2-nistp256|ecdsa-sha2-nistp384|"
    r"ecdsa-sha2-nistp521|sk-ssh-ed25519@openssh\.com|"
    r"sk-ecdsa-sha2-nistp256@openssh\.com) +AAAA[0-9A-Za-z+/=]+( +\S.*)?$"
)


def _ssh_keygen() -> str:
    return os.environ.get("SECUREDCHAT_SSH_KEYGEN", "ssh-keygen")


class SigningError(RuntimeError):
    pass


class SigStatus(Enum):
    VERIFIED = "verified"          # signed by the key pinned for this `from`
    BAD_SIG = "bad-signature"      # a key is pinned but the signature doesn't match (tamper/forgery)
    UNKNOWN_SIGNER = "unknown-signer"  # no pinned key for this `from` (or none verifies)
    UNSIGNED = "unsigned"          # no signature present, and no key pinned for this `from`
    MISSING_EXPECTED_SIG = "missing-expected-signature"  # no signature BUT a key is pinned for this `from` -> downgrade/strip attack
    ERROR = "error"                # ssh-keygen missing / unexpected failure


# A signature blob larger than this is rejected before we touch the filesystem.
# A real armored SSH signature over a small payload is well under 4 KiB; the cap
# stops a hostile multi-GB `sig` from OOMing the process or filling /tmp (the
# cross-family F6 finding — defence-in-depth; the primary fix is an ingest-level
# message-size cap, out of this module's scope).
_MAX_SIG_BYTES = 16 * 1024

# Signature-scheme tags this build understands. `sig_alg` is NOT in the signed
# payload (a wire-format change would break every existing signature), so verify
# must reject an unknown tag rather than trust it — otherwise a future
# alg-dispatch could be steered by unauthenticated metadata (GPT B3).
_KNOWN_SIG_ALGS = (None, "", SIG_ALG)


@dataclass
class VerifyResult:
    status: SigStatus
    detail: str = ""

    @property
    def ok(self) -> bool:
        return self.status is SigStatus.VERIFIED


# ----- paths --------------------------------------------------------------- #

def config_dir() -> Path:
    """Where keys + allowed_signers live. `SECUREDCHAT_HOME` overrides (tests,
    or operators who keep config off the default path)."""
    env = os.environ.get("SECUREDCHAT_HOME")
    if env:
        return Path(env)
    return Path.home() / ".config" / "securedchat"


def key_path(identity: str) -> Path:
    if not _SAFE_NAME.match(identity or ""):
        raise SigningError(f"invalid identity {identity!r}")
    return config_dir() / "keys" / identity


def pub_path(identity: str) -> Path:
    kp = key_path(identity)
    return kp.with_name(kp.name + ".pub")


def allowed_signers_path() -> Path:
    return config_dir() / "allowed_signers"


def have_key(identity: str) -> bool:
    return key_path(identity).exists()


# ----- canonical payload (the one source of truth) ------------------------- #

def canonical_payload(msg) -> bytes:
    """Deterministic bytes signed/verified for `msg`. Covers the full content
    tuple (NOT just body) so a signature can't be lifted onto a different
    recipient/thread/id. Excludes sig/sig_alg (can't sign the signature).

    Both sign and verify call this; a divergence here is the classic homegrown-
    crypto bug (signing X but verifying Y), so it is pinned by a test."""
    d = {
        "ts": msg.ts,
        "id": msg.id,
        "from": msg.from_,
        "to": msg.to,
        "kind": msg.kind,
        "body": msg.body,
        "reply_to": msg.reply_to,
    }
    # allow_nan=False: reject NaN/Infinity rather than emit the non-standard
    # `NaN`/`Infinity` tokens a strict JSON parser would refuse (parser
    # disagreement across sign/verify). Byte-identical for all finite/valid
    # payloads, so NOT a wire-format change; verify() catches the ValueError and
    # returns ERROR instead of crashing on a hostile message.
    return json.dumps(
        d, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode("utf-8")


# ----- key generation ------------------------------------------------------ #

def keygen(identity: str, overwrite: bool = False) -> tuple[Path, str]:
    """Generate (or reuse) a dedicated ed25519 keypair for `identity`.

    Returns (private_key_path, public_key_line). The public line is what the
    operator shares OUT OF BAND so other participants can `trust` it — signing
    only enforces trust; first-contact establishes it (see THREAT_MODEL.md)."""
    kp = key_path(identity)
    kp.parent.mkdir(parents=True, exist_ok=True)
    if kp.exists() and not overwrite:
        return kp, pub_path(identity).read_text(encoding="utf-8").strip()

    # Generate into a temp path FIRST, then atomically swap in. A failed
    # ssh-keygen must NOT destroy the existing working key (GPT B4 escalation:
    # the old code unlinked the key before regenerating, so a regen failure left
    # the identity with no key at all).
    tmp = kp.with_name(kp.name + ".new")
    tmp_pub = tmp.with_name(tmp.name + ".pub")
    for stale in (tmp, tmp_pub):
        stale.unlink(missing_ok=True)
    try:
        proc = subprocess.run(
            [_ssh_keygen(), "-t", "ed25519", "-f", str(tmp), "-N", "",
             "-C", f"{identity}@securedchat", "-q"],
            capture_output=True, text=True,
        )
    except FileNotFoundError:
        tmp.unlink(missing_ok=True)
        tmp_pub.unlink(missing_ok=True)
        raise SigningError("ssh-keygen not found (keygen needs OpenSSH >= 8.2)")
    if proc.returncode != 0:
        tmp.unlink(missing_ok=True)
        tmp_pub.unlink(missing_ok=True)
        raise SigningError(f"keygen failed: {proc.stderr.strip() or proc.stdout.strip()}")
    try:
        os.chmod(tmp, 0o600)  # best-effort; no-op semantics on Windows
    except OSError:
        pass
    # Only now that the new key exists, replace the old one.
    os.replace(tmp, kp)
    os.replace(tmp_pub, pub_path(identity))
    return kp, pub_path(identity).read_text(encoding="utf-8").strip()


# ----- sign / verify ------------------------------------------------------- #

def sign(msg, identity: str | None = None, key: Path | None = None) -> str:
    """Return the armored SSH signature for `msg`'s canonical payload. Pass an
    explicit `key` path, or `identity` to use that identity's pinned key."""
    kp = Path(key) if key is not None else key_path(identity or msg.from_)
    if not kp.exists():
        raise SigningError(f"no signing key at {kp} (run `keygen` first)")
    try:
        proc = subprocess.run(
            [_ssh_keygen(), "-Y", "sign", "-f", str(kp), "-n", NAMESPACE],
            input=canonical_payload(msg), capture_output=True,
        )
    except FileNotFoundError:
        raise SigningError("ssh-keygen not found (signing needs OpenSSH >= 8.2)")
    if proc.returncode != 0:
        raise SigningError(f"sign failed: {proc.stderr.decode('utf-8', 'replace').strip()}")
    return proc.stdout.decode("utf-8")


def verify(msg, signers: Path | None = None) -> VerifyResult:
    """Verify `msg`'s signature against the pinned `allowed_signers`, binding the
    principal to `msg.from_`. Returns a classified VerifyResult; the caller's
    policy (off/warn/strict) decides what to do with each status."""
    sig = getattr(msg, "sig", None)
    if not sig:
        # A missing signature is benign ONLY if no key is pinned for this
        # principal. If a key IS pinned, an unsigned message claiming that
        # `from_` is a downgrade/strip attack — surface it distinctly so `warn`
        # policy flags it instead of treating it as an ordinary unsigned message
        # (the 3/3 cross-family release-blocker F5/B1). `is_pinned` reads the same
        # allowed_signers file the signed path checks against.
        if is_pinned(getattr(msg, "from_", "") or ""):
            return VerifyResult(SigStatus.MISSING_EXPECTED_SIG,
                                f"{msg.from_!r} is pinned but the message is unsigned")
        return VerifyResult(SigStatus.UNSIGNED)

    # `sig_alg` is unauthenticated metadata (deliberately not in the signed
    # payload — binding it is a wire-format change, tracked as a tag-blocker).
    # Reject an unknown tag rather than let it steer future alg-dispatch (GPT B3).
    if getattr(msg, "sig_alg", None) not in _KNOWN_SIG_ALGS:
        return VerifyResult(SigStatus.BAD_SIG, f"unknown sig_alg {getattr(msg, 'sig_alg', None)!r}")

    # Reject an oversized signature before it touches memory/disk (F6 DoS guard).
    if len(sig) > _MAX_SIG_BYTES:
        return VerifyResult(SigStatus.BAD_SIG,
                            f"signature too large ({len(sig)} bytes > {_MAX_SIG_BYTES})")

    signers = signers or allowed_signers_path()
    if not signers.exists():
        return VerifyResult(SigStatus.UNKNOWN_SIGNER, "no allowed_signers file")
    if not _SAFE_NAME.match(msg.from_ or ""):
        return VerifyResult(SigStatus.BAD_SIG, f"unsafe from {msg.from_!r}")

    try:
        payload = canonical_payload(msg)
    except (ValueError, TypeError) as exc:
        # A hostile message with a non-finite float or lone Unicode surrogate must
        # not crash verify — classify as ERROR (GPT canonicalisation finding).
        return VerifyResult(SigStatus.ERROR, f"uncanonicalisable payload: {exc}")

    tf = tempfile.NamedTemporaryFile(
        "w", suffix=".sig", delete=False, encoding="utf-8", newline="\n")
    try:
        tf.write(sig)
        tf.close()
        proc = subprocess.run(
            [_ssh_keygen(), "-Y", "verify", "-f", str(signers),
             "-I", msg.from_, "-n", NAMESPACE, "-s", tf.name],
            input=payload, capture_output=True,
        )
    except FileNotFoundError:
        return VerifyResult(SigStatus.ERROR, "ssh-keygen not found")
    finally:
        try:
            os.unlink(tf.name)
        except OSError:
            pass

    if proc.returncode == 0:
        return VerifyResult(SigStatus.VERIFIED)
    err = proc.stderr.decode("utf-8", "replace").strip()
    # A pinned key exists but the bytes don't match → tamper/forgery. ssh-keygen
    # says "incorrect signature". No pinned key for the principal → just "Could
    # not verify". Distinguishing the two only affects the message we print;
    # both REJECT under strict.
    if "incorrect signature" in err.lower():
        return VerifyResult(SigStatus.BAD_SIG, err)
    return VerifyResult(SigStatus.UNKNOWN_SIGNER, err)


# ----- allowed_signers (the pins file = authorized_keys analog) ------------ #

def _read_signers_lines() -> list[str]:
    p = allowed_signers_path()
    if not p.exists():
        return []
    return [ln for ln in p.read_text(encoding="utf-8").splitlines() if ln.strip()]


def add_pin(principal: str, pubkey_line: str) -> bool:
    """Pin `principal`'s public key (first-contact / key-roll). Returns True if a
    new entry was written, False if that exact line was already present.

    Multiple keys per principal are allowed (both verify) — that's how a key roll
    works without a flag day: pin the new key, drop the old once everyone's
    migrated. Validation rejects anything that could inject an allowed_signers
    line (embedded newline) or an unknown key type."""
    principal = (principal or "").strip()
    pubkey_line = (pubkey_line or "").strip()
    if not _SAFE_NAME.match(principal):
        raise SigningError(f"invalid principal {principal!r}: letters, digits, . _ - only")
    # Reject ANY control character (NUL / TAB / etc.) before the shape check —
    # `.` in _PUBKEY_RE's comment group otherwise admits embedded NUL/TAB, which
    # can corrupt the allowed_signers line or downstream tooling (GPT trust-store
    # integrity finding). Full real-key validation (`ssh-keygen -l`) is a heavier
    # follow-up; this closes the cheap, concrete hole now.
    if any(ord(c) < 0x20 or ord(c) == 0x7f for c in pubkey_line):
        raise SigningError("public key line contains control characters")
    if not _PUBKEY_RE.match(pubkey_line):
        raise SigningError("invalid public key line (expected `ssh-ed25519 AAAA... [comment]`)")
    entry = f"{principal} {pubkey_line}"
    existing = _read_signers_lines()
    if entry in existing:
        return False
    p = allowed_signers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    with p.open("a", encoding="utf-8", newline="\n") as f:
        f.write(entry + "\n")
    return True


def _atomic_write_signers(lines: list[str]) -> None:
    """Write allowed_signers atomically (temp + os.replace) so a crash mid-write
    can't truncate the trust root (GPT trust-store atomicity finding)."""
    p = allowed_signers_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    content = ("\n".join(lines) + "\n") if lines else ""
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(content, encoding="utf-8", newline="\n")
    os.replace(tmp, p)


def remove_pin(principal: str, key: str | None = None) -> int:
    """Revoke pinned keys for `principal`. Returns the number removed.

    `key=None`  → drop ALL entries for `principal` (full revoke).
    `key` set   → drop ONLY the matching entry (a full `ssh-ed25519 AAAA... [cmt]`
                  line or its bare base64 blob), leaving the principal's OTHER keys
                  pinned. This is what makes a staggered key-roll ("pin new, drop
                  old once migrated") and single-key compromise recovery possible;
                  the old API could only wipe the principal entirely, forcing a DoS
                  window (the 3/3 cross-family F3 release-blocker).

    (The residual-risk window — peers who haven't pulled the removal still accept
    the old key — is named in THREAT_MODEL.md.)"""
    principal = (principal or "").strip()
    target_blob = None
    if key is not None:
        # Match on the base64 blob so a differing trailing comment doesn't miss.
        parts = key.strip().split()
        target_blob = next((p for p in parts if p.startswith("AAAA")), key.strip())

    lines = _read_signers_lines()
    kept: list[str] = []
    for ln in lines:
        fields = ln.split()
        ln_principal = fields[0] if fields else ""
        if ln_principal != principal:
            kept.append(ln)
            continue
        if target_blob is not None:
            ln_blob = next((f for f in fields[1:] if f.startswith("AAAA")), None)
            if ln_blob != target_blob:
                kept.append(ln)          # same principal, different key → keep
                continue
        # key is None (wipe-all) OR the blob matched → drop this line
    removed = len(lines) - len(kept)
    if removed:
        _atomic_write_signers(kept)
    return removed


def list_pins() -> list[tuple[str, str, str]]:
    """Pinned (principal, keytype, key-prefix) tuples, for `trusted`."""
    out: list[tuple[str, str, str]] = []
    for ln in _read_signers_lines():
        parts = ln.split(None, 2)
        if len(parts) >= 3:
            principal, keytype, keydata = parts[0], parts[1], parts[2]
            prefix = keydata.split()[0][:24] if keydata else ""
            out.append((principal, keytype, prefix))
    return out


def is_pinned(principal: str) -> bool:
    principal = (principal or "").strip()
    return any((ln.split(None, 1) or [""])[0] == principal for ln in _read_signers_lines())
