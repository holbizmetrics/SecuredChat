"""bus_receive_files.py — receive [FILE-TRANSFER: ...] payloads from a room log.

The receiver half of the COOKBOOK Recipe 10 wire contract (step 6): header line
`[FILE-TRANSFER: <relpath> — <note>] sha256=<hex> bytes=<n> ...`, payload between
-----BEGIN FILE----- / -----END FILE-----. Three invariants, all load-bearing:

  1. LENGTH-DELIMITED extraction — payload is the first <bytes> utf-8 bytes after the
     BEGIN marker, so payloads that themselves contain marker-like text transfer intact.
  2. SHA-ON-WRITTEN-BYTES — the hash is re-computed from the file on disk after writing;
     a byte is trusted only once the written artifact matches the announced sha256.
  3. PATH JAIL — targets resolve strictly under the staging dir; absolute paths, drive
     colons, backslashes, and `..` components are refused (backslash matters: a POSIX
     parse treats `a\\..\\b` as one component, but a Windows write would walk it).

BINARY SUPPORT (added 2026-07-25, opt-in, backward compatible): an optional header
field `encoding=base64` makes the payload base64 TEXT that decodes to the real file.
Without it the payload is raw utf-8 exactly as before, so every existing transfer is
unaffected.

  WHY: the raw path is TEXT-ONLY BY CONSTRUCTION -- it takes the message body as utf-8
  bytes, and a PNG's bytes cannot survive a JSON string round-trip. The gap went
  unnoticed because every transfer so far was source code (the 12-file csbus tree, all
  text). Found 2026-07-25 while relaying a screenshot to a phone session.

  With `encoding=base64`, `bytes=` is the size of the DECODED file and `sha256=` is the
  hash of the DECODED file -- the artifact you actually wanted. That STRENGTHENS
  invariant 2 rather than weakening it: the check still happens on the written bytes,
  and it now checks the real file rather than its transport encoding. Invariant 1 is
  preserved differently: base64 cannot collide with the marker text, so the payload is
  everything after BEGIN with whitespace stripped, and the decoded LENGTH is asserted
  against `bytes=` before the hash is considered.

Provenance: generalized 2026-07-21 from session a7d4ea17's proven one-shot receiver
(the 19/19 deterministic-terminal interop transfers). Corpus: test_bus_receive_files.py.

Usage:
  python bus_receive_files.py [--bus PATH] [--room NAME] [--from-identity ID]
                              [--staging DIR]
  Env fallbacks: SECUREDCHAT_BUS, SECUREDCHAT_ROOM (same as chat.py).
  Exit 0 = no failures (received count may be 0); 1 = any failure; 2 = config error.
"""
from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import pathlib
import re
import sys

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

HEADER_RE = re.compile(
    r"\[FILE-TRANSFER: (.+?)(?: [—-] .*?)?\] sha256=([0-9a-f]{64}) bytes=(\d+)")
# Opt-in binary transport. Absent => raw utf-8 payload, exactly as before.
ENCODING_RE = re.compile("encoding=([A-Za-z0-9_-]+)")
BEGIN_MARKER = "-----BEGIN FILE-----\n"


def jail_reject_reason(relpath):
    """Non-empty string = why the path is refused; None = safe under staging."""
    if "\\" in relpath:
        return "backslash in path"
    if ":" in relpath:
        return "drive colon in path"
    parts = pathlib.PurePosixPath(relpath)
    if parts.is_absolute():
        return "absolute path"
    if ".." in parts.parts:
        return "parent-directory component"
    if not parts.parts:
        return "empty path"
    return None


def receive(log_path, staging_dir, from_identity=None):
    """Scan the room log; write verified payloads under staging_dir.
    Returns (ok, fail) lists of (msg_id8, relpath, detail). Idempotent re-runs."""
    ok, fail = [], []
    staging = pathlib.Path(staging_dir)
    for line in pathlib.Path(log_path).read_text(encoding="utf-8").splitlines():
        try:
            message = json.loads(line)
        except json.JSONDecodeError:
            continue
        body = message.get("body", "")
        if "[FILE-TRANSFER:" not in body:
            continue
        if from_identity and message.get("from") != from_identity:
            continue
        msg_id = str(message.get("id", "?"))[:8]
        header = HEADER_RE.search(body)
        if not header:
            fail.append((msg_id, "?", "header unparseable")); continue
        relpath, want_sha, nbytes = header.group(1), header.group(2), int(header.group(3))
        reason = jail_reject_reason(relpath)
        if reason:
            fail.append((msg_id, relpath, f"path rejected ({reason})")); continue
        begin = body.find(BEGIN_MARKER)
        if begin < 0:
            fail.append((msg_id, relpath, "no BEGIN marker")); continue
        # NB: HEADER_RE's match ENDS at bytes=<n>, so encoding= lives OUTSIDE
        # header.group(0). Search the header LINE. (Got this wrong first pass:
        # the branch silently never ran and every binary case fell through to
        # the raw path -- caught only because the corpus asserted the reason.)
        header_line = body[:body.find(chr(10))] if chr(10) in body else body
        enc_m = ENCODING_RE.search(header_line)
        encoding = (enc_m.group(1).lower() if enc_m else "raw")
        # `encoding=utf-8` is ALREADY the live wire contract -- 21 real transfers on
        # the bus carry it, and the shipped corpus emits it. Treating it as unknown
        # refused every existing transfer; the corpus caught that regression before
        # it shipped. Text encodings all take the raw path; only base64 branches.
        if encoding not in ("raw", "utf-8", "utf8", "base64"):
            fail.append((msg_id, relpath, f"unknown encoding={encoding}")); continue

        if encoding == "base64":
            # base64 cannot collide with the marker text, so length-delimiting the
            # TRANSPORT is unnecessary; instead the DECODED length is asserted against
            # bytes= before the hash is considered. Same trust shape, real artifact.
            raw_text = body[begin + len(BEGIN_MARKER):]
            try:
                payload = base64.b64decode("".join(raw_text.split()), validate=True)
            except Exception as e:
                fail.append((msg_id, relpath, f"base64 decode failed: {e}")); continue
            if len(payload) != nbytes:
                fail.append((msg_id, relpath,
                             f"decoded length {len(payload)} != announced {nbytes}")); continue
        else:
            payload = body[begin + len(BEGIN_MARKER):].encode("utf-8")[:nbytes]
            if len(payload) < nbytes:
                fail.append((msg_id, relpath, f"payload short: {len(payload)}/{nbytes} bytes"))
                continue
        if hashlib.sha256(payload).hexdigest() != want_sha:
            fail.append((msg_id, relpath, "sha mismatch (extracted)")); continue
        dest = staging / pathlib.PurePosixPath(relpath)
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(payload)
        written_sha = hashlib.sha256(dest.read_bytes()).hexdigest()
        if written_sha != want_sha:
            fail.append((msg_id, relpath, "sha mismatch (written file)")); continue
        ok.append((msg_id, relpath, nbytes))
    return ok, fail


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--bus", default=os.environ.get("SECUREDCHAT_BUS"))
    parser.add_argument("--room", default=os.environ.get("SECUREDCHAT_ROOM"))
    parser.add_argument("--from-identity", default=None,
                        help="only accept transfers from this sender")
    parser.add_argument("--staging", default="csbus-staging",
                        help="directory files are jailed under (default ./csbus-staging)")
    args = parser.parse_args(argv)
    if not args.bus or not args.room:
        print("missing config: --bus/--room (or env SECUREDCHAT_BUS / SECUREDCHAT_ROOM)")
        return 2
    log_path = pathlib.Path(args.bus) / args.room / "chat.jsonl"
    if not log_path.exists():
        print(f"no room log at {log_path}")
        return 2
    ok, fail = receive(log_path, args.staging, args.from_identity)
    print(f"RECEIVED OK: {len(ok)}")
    for msg_id, relpath, nbytes in ok:
        print(f"  PASS {msg_id} {relpath} ({nbytes} bytes)")
    print(f"FAILED: {len(fail)}")
    for msg_id, relpath, why in fail:
        print(f"  FAIL {msg_id} {relpath}: {why}")
    return 1 if fail else 0


if __name__ == "__main__":
    sys.exit(main())
