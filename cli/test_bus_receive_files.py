"""test_bus_receive_files.py — birth battery for the shipped FILE-TRANSFER receiver.

Discrimination in both directions: valid transfers land byte-exact; every hostile or
malformed shape is refused with a named reason. The marker-in-payload case is the point
of length-delimited extraction; the backslash case is the Windows jail-escape the
one-shot ancestor never needed to face.

Run:  python test_bus_receive_files.py   (pure-local, synthetic log, <1s)
"""
from __future__ import annotations

import hashlib
import json
import pathlib
import sys
import tempfile

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

sys.path.insert(0, str(pathlib.Path(__file__).parent))
from bus_receive_files import BEGIN_MARKER, jail_reject_reason, receive  # noqa: E402

FAILURES = []


def check(name, condition, detail=""):
    status = "PASS" if condition else "FAIL"
    print(f"  [{status}] {name}" + (f" — {detail}" if detail else ""))
    if not condition:
        FAILURES.append(name)


def transfer_message(sender, relpath, payload_text, sha=None, nbytes=None, msg_id="m1"):
    raw = payload_text.encode("utf-8")
    sha = sha or hashlib.sha256(raw).hexdigest()
    nbytes = len(raw) if nbytes is None else nbytes
    body = (f"[FILE-TRANSFER: {relpath} — flow-back test] sha256={sha} bytes={nbytes} "
            f"encoding=utf-8\n{BEGIN_MARKER}{payload_text}\n-----END FILE-----")
    return {"id": msg_id, "from": sender, "body": body}


def main():
    workdir = pathlib.Path(tempfile.mkdtemp(prefix="busrecv-test-"))
    log = workdir / "chat.jsonl"
    staging = workdir / "staging"

    tricky_payload = ("line one\n-----BEGIN FILE-----\nfake inner marker\nline last\n"
                      "unicode: Grüße\n")
    good = transfer_message("web-claude-0d61", "sub/dir/good.txt", tricky_payload,
                            msg_id="good0001")
    bad_sha = transfer_message("web-claude-0d61", "bad.txt", "content", sha="0" * 64,
                               msg_id="badsha01")
    escape_dots = transfer_message("web-claude-0d61", "../evil.txt", "x", msg_id="escdots1")
    escape_back = transfer_message("web-claude-0d61", "a\\..\\evil.txt", "x", msg_id="escback1")
    escape_abs = transfer_message("web-claude-0d61", "/etc/evil", "x", msg_id="escabs01")
    escape_drive = transfer_message("web-claude-0d61", "C:/evil.txt", "x", msg_id="escdrv01")
    short_payload = transfer_message("web-claude-0d61", "short.txt", "abc", nbytes=99,
                                     msg_id="short001")
    other_sender = transfer_message("someone-else", "other.txt", "hello", msg_id="othersnd")
    lines = [json.dumps(m) for m in
             (good, bad_sha, escape_dots, escape_back, escape_abs, escape_drive,
              short_payload, other_sender)]
    lines.insert(0, "not json at all {{{")
    lines.insert(2, json.dumps({"id": "chat0001", "from": "x", "body": "plain chat line"}))
    log.write_text("\n".join(lines), encoding="utf-8")

    print("[1] filtered run (--from-identity web-claude-0d61)")
    ok, fail = receive(log, staging, from_identity="web-claude-0d61")
    check("exactly the good transfer lands", [row[1] for row in ok] == ["sub/dir/good.txt"],
          f"ok={ok}")
    landed = (staging / "sub/dir/good.txt").read_bytes()
    check("written bytes are byte-exact (marker-in-payload survives)",
          landed == tricky_payload.encode("utf-8"))
    reasons = {row[1]: row[2] for row in fail}
    check("bad sha refused", "sha mismatch" in reasons.get("bad.txt", ""), str(reasons))
    check("dot-dot path jailed", "parent-directory" in reasons.get("../evil.txt", ""))
    check("backslash path jailed (Windows escape)",
          "backslash" in reasons.get("a\\..\\evil.txt", ""))
    check("absolute path jailed", "absolute" in reasons.get("/etc/evil", ""))
    check("drive-colon path jailed", "colon" in reasons.get("C:/evil.txt", ""))
    check("short payload refused, not padded", "payload short" in reasons.get("short.txt", ""))
    check("other sender excluded by filter", "other.txt" not in reasons
          and all(row[1] != "other.txt" for row in ok))
    check("nothing hostile reached staging",
          sorted(p.name for p in staging.rglob("*") if p.is_file()) == ["good.txt"])

    print("[2] unfiltered run accepts any sender")
    ok2, _ = receive(log, staging, from_identity=None)
    check("other sender's file lands without filter",
          any(row[1] == "other.txt" for row in ok2))

    print("[3] jail unit checks")
    check("plain relative path allowed", jail_reject_reason("a/b/c.txt") is None)
    check("empty path refused", jail_reject_reason("") is not None)

    print()
    if FAILURES:
        print(f"BATTERY: {len(FAILURES)} FAILURE(S) -> " + ", ".join(FAILURES))
        sys.exit(1)
    print("BATTERY: ALL GREEN. Receiver discriminates; jail holds on all four escape shapes.")


if __name__ == "__main__":
    main()
