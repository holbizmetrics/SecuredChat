"""test_signers_principals.py - a multi-principal allowed_signers line pins every principal on it."""
import sys, pathlib, tempfile, os
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
FAILS = []
def check(name, cond):
    if not cond: FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")
def main():
    with tempfile.TemporaryDirectory() as tmp:
        os.environ["SECUREDCHAT_HOME"] = tmp
        import signing
        signing.allowed_signers_path().parent.mkdir(parents=True, exist_ok=True)
        signing.allowed_signers_path().write_text("alice,bob ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample000000000000000000000000000 c\ncarol ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIExample111111111111111111111111111 d\n")
        check("THE CASE: second principal on a comma line is pinned", signing.is_pinned("bob"))
        check("first principal on a comma line is pinned", signing.is_pinned("alice"))
        check("single-principal line still pinned", signing.is_pinned("carol"))
        check("CONTROL: an unlisted principal is NOT pinned", not signing.is_pinned("dave"))
        check("CONTROL: the whole comma token is not itself a principal", not signing.is_pinned("alice,bob"))
        pins = [p for p, _, _ in signing.list_pins()]
        check("list_pins expands the comma line to both principals", pins.count("alice") == 1 and pins.count("bob") == 1 and pins.count("carol") == 1)
    if FAILS: print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}"); sys.exit(1)
    print("BATTERY: ALL GREEN. Every principal on an allowed_signers line is pinned.")
if __name__ == "__main__": main()
