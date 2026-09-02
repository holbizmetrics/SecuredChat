"""test_transport_typed_fields.py - the _coerce_ts class, re-opened on four more fields.

REVIEW 2026-09-02 (linux-claude-5534b575): from_jsonl passed `to`, `reply_to`, `sig`,
`sig_alg`, `sig_v` through raw. One row with "to": 5 raised TypeError inside
_addressed_to for every reader running `recv --addressed-to-me`; "sig": 1 raised inside
verify under any --verify-sig policy; `main` catches only RuntimeError/OSError.
Same principle as test_transport_ts_coercion: a transport degrades on a ROW, never on
the LOG. Also locks: a non-string `to` must NOT become a broadcast (None).

Plus the _git bound: a hung git call must come back as rc=124 with a stderr that names
the timeout (check=False) or raise RuntimeError (check=True) -- never hang, never vanish.
"""
import os, sys, pathlib, json, subprocess, tempfile
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import transport  # noqa: E402
from transport import Message, _coerce_opt_str  # noqa: E402

FAILS = []
def check(name, cond, detail=""):
    if not cond: FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))

def main():
    print("[typed optional fields - never raise, never broadcast by accident]")
    check("None stays None", _coerce_opt_str(None) is None)
    check("str passes through", _coerce_opt_str("windows-claude") == "windows-claude")
    check("int becomes str, not None", _coerce_opt_str(5) == "5")
    check("list becomes str, not None", isinstance(_coerce_opt_str(["a"]), str))
    check("bool becomes str", _coerce_opt_str(True) == "True")

    # THE INCIDENT SHAPE, end to end through from_jsonl
    row = {"ts": 1.0, "id": "x" * 36, "from": "peer", "to": 5, "body": "hi",
           "reply_to": 7, "sig": 1, "sig_alg": 2, "sig_v": 3}
    try:
        m = Message.from_jsonl(json.dumps(row)); ok = True
    except Exception as e:
        ok = False; m = None; print("   raised:", repr(e))
    check("THE INCIDENT: off-type to/reply_to/sig/sig_alg/sig_v parse without raising", ok)
    if m:
        check("to='5' (a string that addresses nobody), NOT None/broadcast", m.to == "5")
        check("reply_to='7'", m.reply_to == "7")
        check("sig='1' so verify sees a malformed sig, not a crash", m.sig == "1")
        # the downstream call that crashed in the incident
        check("identity.startswith(to + '-') no longer raises", not "me-x".startswith(m.to + "-"))
        check("m.reply_to[:8] no longer raises", m.reply_to[:8] == "7")
    # sig_v is the one field coerced to INT, not str: it is compared `== 2` in
    # canonical_payload, so "2" (str) silently stopped matching v2 -- caught by
    # test_chat's wire roundtrip at merge time (termux, 2026-09-02).
    m_v = Message.from_jsonl(json.dumps({"id": "y1", "ts": 1.0, "from": "a",
                                         "body": "b", "sig_v": "2"}))
    check("sig_v: int-looking str parses to int 2", m_v.sig_v == 2)
    m_v = Message.from_jsonl(json.dumps({"id": "y2", "ts": 1.0, "from": "a",
                                         "body": "b", "sig_v": [2]}))
    check("sig_v: junk degrades to None (legacy/absent, fail-closed), never a fake version",
          m_v.sig_v is None)
    # negative control: a well-formed row is untouched
    good = {"ts": 1.0, "id": "y" * 36, "from": "a", "to": "b", "body": "", "reply_to": None}
    g = Message.from_jsonl(json.dumps(good))
    check("CONTROL: well-formed row unchanged (to='b', reply_to None, sig None)", g.to == "b" and g.reply_to is None and g.sig is None)

    print("[_git is bounded]")
    with tempfile.TemporaryDirectory() as d:
        subprocess.run(["git", "init", "-q", d], check=True)
        pathlib.Path(d, ".securedchat-bus").write_text("x")
        t = transport.GitBusTransport(pathlib.Path(d), "room", "me") if hasattr(transport, "GitBusTransport") else None
        if t is None:
            # find the transport class generically
            cls = [v for k, v in vars(transport).items() if isinstance(v, type) and hasattr(v, "_git")][0]
            t = cls(pathlib.Path(d), "room", "me")
        # a git call that blocks REGARDLESS of the harness's stdin. The first version
        # used `credential fill`, which only hangs when the inherited stdin is an open
        # tty/pipe -- under a /dev/null harness (termux, 2026-09-02) git exits 128
        # instantly and the arm tested the harness, not the bound. `config --edit`
        # waits for the editor to exit; a sleeping python editor is a portable hang.
        os.environ["GIT_EDITOR"] = f'"{sys.executable}" -c "import time; time.sleep(30)"'
        try:
            r = t._git("config", "--edit", check=False, timeout=1.0)
            check("hung git returns rc=124 under check=False", r.returncode == 124, f"rc={r.returncode}")
            check("stderr NAMES the timeout", "timed out" in (r.stderr or ""), r.stderr[:80])
            raised = False
            try:
                t._git("config", "--edit", check=True, timeout=1.0)
            except RuntimeError as e:
                raised = "timed out" in str(e)
            check("hung git raises RuntimeError under check=True", raised)
        finally:
            os.environ.pop("GIT_EDITOR", None)
        # CONTROL: a fast call is unaffected and the env carries GIT_TERMINAL_PROMPT=0
        r = t._git("rev-parse", "--is-inside-work-tree", check=False, timeout=30)
        check("CONTROL: normal git call still rc=0", r.returncode == 0, r.stderr[:80])
        check("GIT_TIMEOUT default is positive", transport.GIT_TIMEOUT > 0)

    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}"); sys.exit(1)
    print("BATTERY: ALL GREEN. Off-type fields degrade on the row; every git call is bounded.")

if __name__ == "__main__":
    main()
