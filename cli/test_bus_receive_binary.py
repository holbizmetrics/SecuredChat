"""test_bus_receive_binary.py — birth battery for `encoding=base64` binary transfer.

Separate from test_bus_receive_files.py on purpose: that file is the shipped
text-path corpus and stays untouched, so a regression in the binary feature can
never be confused with a regression in the transport that already works.

NULL-CONTROL-AT-BIRTH. Every arm carries a planted positive AND a known-clean
negative, and each negative asserts the REASON, not merely that it failed. That
matters here: during development three cases "passed" while reporting raw-path
errors — the base64 branch was never running, because the encoding field lives
OUTSIDE HEADER_RE's match and my lookup searched the match. A pass-by-wrong-reason
is a corpus lying to you.

THE LOAD-BEARING NEGATIVE is `same binary WITHOUT encoding=base64 FAILS`. That is
the gap the feature closes. If raw transport also carried binary intact, the
feature would be pointless and this corpus would be decoration.

Run:  python test_bus_receive_binary.py   (pure-local, synthetic log, <1s)
"""
from __future__ import annotations

import base64
import hashlib
import json
import pathlib
import sys
import tempfile

try:  # cp1252 guard: this file prints non-ASCII (em-dash) in failure detail
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from bus_receive_files import BEGIN_MARKER, receive  # noqa: E402

FAILS: list[str] = []

# Every byte value including NUL, built escape-free. A literal like b"\x89PNG\r\n"
# does not survive round-tripping through shell heredocs; this construction does.
BINARY = (bytes([137, 80, 78, 71, 13, 10, 26, 10])      # PNG magic
          + bytes(range(256)) * 3                        # all 256 byte values
          + bytes([255, 254, 0]) + b"tail" + bytes([0]))


def check(name, condition, detail=""):
    ok = bool(condition)
    if not ok:
        FAILS.append(name)
    print(f"  [{'PASS' if ok else 'FAIL'}] {name}" + (f" — {detail}" if detail and not ok else ""))


def msg(relpath, raw, encoding="base64", sha=None, nbytes=None, payload=None, mid="b1"):
    sha = sha if sha is not None else hashlib.sha256(raw).hexdigest()
    nbytes = nbytes if nbytes is not None else len(raw)
    payload = payload if payload is not None else base64.b64encode(raw).decode()
    enc = f" encoding={encoding}" if encoding else ""
    body = (f"[FILE-TRANSFER: {relpath} — binary test] sha256={sha} bytes={nbytes}{enc}"
            + chr(10) + BEGIN_MARKER + payload)
    return {"id": mid, "from": "peer", "body": body}


def run(messages):
    d = pathlib.Path(tempfile.mkdtemp(prefix="busrecv-bin-"))
    log = d / "chat.jsonl"
    log.write_text("".join(json.dumps(m) + chr(10) for m in messages), encoding="utf-8")
    staging = d / "staging"
    ok, fail = receive(log, staging, from_identity=None)
    return ok, fail, staging


def main() -> int:
    print("[binary via encoding=base64]")

    ok, fail, staging = run([msg("img/shot.png", BINARY)])
    check("base64 binary transfer lands", len(ok) == 1 and not fail, f"{ok} {fail}")
    if ok:
        got = (staging / "img" / "shot.png").read_bytes()
        check("written file is BYTE-IDENTICAL to the source (all 256 values, NUL included)",
              got == BINARY, f"{len(got)} vs {len(BINARY)}")

    # THE load-bearing negative: identical bytes, no encoding field -> raw path -> must fail.
    ok, fail, _ = run([msg("img/raw.png", BINARY, encoding=None,
                           payload=base64.b64encode(BINARY).decode())])
    check("same binary WITHOUT encoding=base64 FAILS (the gap this closes)",
          not ok and len(fail) == 1, f"{ok} {fail}")

    # text transfers must be untouched by the new branch
    text = "hello\nwith a -----BEGIN FILE----- inside\n"
    ok, fail, staging = run([msg("t/a.txt", text.encode(), encoding="utf-8",
                                 payload=text)])
    check("encoding=utf-8 still takes the RAW path and lands (live wire contract)",
          len(ok) == 1 and not fail, f"{ok} {fail}")

    ok, fail, _ = run([msg("img/bad.png", BINARY, payload="!!! not base64 !!!")])
    check("corrupt base64 is refused AS A DECODE FAILURE",
          not ok and fail and "base64 decode failed" in fail[0][2], f"{fail}")

    ok, fail, _ = run([msg("img/len.png", BINARY, nbytes=len(BINARY) + 5)])
    check("decoded-length mismatch is refused AS A LENGTH ERROR",
          not ok and fail and "decoded length" in fail[0][2], f"{fail}")

    ok, fail, _ = run([msg("img/sha.png", BINARY, sha="0" * 64)])
    check("wrong sha is refused (trust invariant holds on the DECODED artifact)",
          not ok and fail and "sha mismatch" in fail[0][2], f"{fail}")

    ok, fail, _ = run([msg("img/x.png", BINARY, encoding="rot13")])
    check("unknown encoding is refused AS UNKNOWN (not silently treated as raw)",
          not ok and fail and "unknown encoding" in fail[0][2], f"{fail}")

    ok, fail, _ = run([msg("../evil.png", BINARY)])
    check("path jail still holds on the binary path",
          not ok and fail and "parent-directory" in fail[0][2], f"{fail}")

    print()
    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}")
        return 1
    print("BATTERY: ALL GREEN. Binary lands byte-exact; raw path unchanged; jail holds.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
