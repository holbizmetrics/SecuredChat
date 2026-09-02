"""test_busid_pin.py - the bus-id binding pins on first sight and holds against a swapped repo file."""
import sys, pathlib, tempfile, os, io, contextlib, subprocess
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
FAILS = []
def check(name, cond, detail=""):
    if not cond: FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))
class T:  # minimal transport stand-in: _sig_ctx reads .root and .room only
    def __init__(self, root): self.root = root; self.room = "r"
def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SECUREDCHAT_HOME"] = str(pathlib.Path(tmp, "home"))
        import chat  # after HOME is set
        root = pathlib.Path(tmp, "bus"); root.mkdir()
        (root / "bus-id").write_text("BUS-X-1234567890\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            room, bus = chat._sig_ctx(T(root))
        check("first sight: value returned unchanged and PINNED", bus == "BUS-X-1234567890" and "PINNED on first sight" in err.getvalue())
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            room, bus2 = chat._sig_ctx(T(root))
        check("second read: same value, no new pin message", bus2 == "BUS-X-1234567890" and "PINNED on first sight" not in err.getvalue())
        # THE ATTACK: swap the repo file for another bus's id
        (root / "bus-id").write_text("BUS-Y-ATTACKER00\n")
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            room, bus3 = chat._sig_ctx(T(root))
        check("THE ATTACK: swapped repo bus-id -> verification binds to the PIN, not the repo", bus3 == "BUS-X-1234567890")
        check("THE ATTACK: an ALERT names both ids", "ALERT" in err.getvalue() and "differs" in err.getvalue())
        # CONTROL: a legacy bus with no bus-id file pins nothing and returns ''
        root2 = pathlib.Path(tmp, "bus2"); root2.mkdir()
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            room, bus4 = chat._sig_ctx(T(root2))
        check("CONTROL: no bus-id file -> '' and no pin written", bus4 == "" and err.getvalue() == "")
        # CONTROL: a different bus root gets its own pin (no cross-bus collision)
        root3 = pathlib.Path(tmp, "bus3"); root3.mkdir(); (root3 / "bus-id").write_text("BUS-Z-000000000\n")
        with contextlib.redirect_stderr(io.StringIO()):
            room, bus5 = chat._sig_ctx(T(root3))
        check("CONTROL: another bus root pins independently", bus5 == "BUS-Z-000000000")
    if FAILS: print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}"); sys.exit(1)
    print("BATTERY: ALL GREEN. bus-id pins on first sight; a swapped repo file cannot re-bind verification.")
if __name__ == "__main__": main()
