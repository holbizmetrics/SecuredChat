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
