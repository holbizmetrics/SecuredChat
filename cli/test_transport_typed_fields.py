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
import sys, pathlib, json, subprocess, tempfile
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
        # a git subcommand that blocks: `git credential fill` waits on stdin; with stdin captured
        # and no input it blocks until timeout -> the bound must fire.
        r = t._git("credential", "fill", check=False, timeout=1.0)
        check("hung git returns rc=124 under check=False", r.returncode == 124, f"rc={r.returncode}")
        check("stderr NAMES the timeout", "timed out" in (r.stderr or ""), r.stderr[:80])
        raised = False
        try:
            t._git("credential", "fill", check=True, timeout=1.0)
        except RuntimeError as e:
            raised = "timed out" in str(e)
        check("hung git raises RuntimeError under check=True", raised)
        # CONTROL: a fast call is unaffected and the env carries GIT_TERMINAL_PROMPT=0
        r = t._git("rev-parse", "--is-inside-work-tree", check=False, timeout=30)
        check("CONTROL: normal git call still rc=0", r.returncode == 0, r.stderr[:80])
        check("GIT_TIMEOUT default is positive", transport.GIT_TIMEOUT > 0)

    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}"); sys.exit(1)
    print("BATTERY: ALL GREEN. Off-type fields degrade on the row; every git call is bounded.")

if __name__ == "__main__":
    main()
