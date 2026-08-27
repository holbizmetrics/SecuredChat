"""test_transport_pull_warning.py - regression battery for the SELF-DISGUISE defect
(courier row 124, deliberately left out of the 2026-08-01 fault-A patch).

DEFECT: _pull_rebase's warning did msg[:200] on git's raw output. Git prefixes
~102-107 chars of "From <origin>" + ref boilerplate, leaving ~93-98 chars for
the fault text. Every box in the 2026-08-01 investigation happened to have
margin (98/96/93 against a 43-char clause); repointing the bus at any longer
remote (org rename, SSH URL, local path) would have silently stripped the
fault's IDENTITY from the warning -- filed as a harmless URL edit.

PRINCIPLE: the fault text must survive regardless of remote-URL length, and
truncation must announce itself. Enforced by _summarize_git_failure (pure).
"""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import _summarize_git_failure, _is_git_noise  # noqa: E402

FAILS = []


def check(name, cond, detail=""):
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}" + (f" - {detail}" if detail and not cond else ""))


FAULT = "Cannot rebase onto multiple branches"  # the 43-char fault-A clause

# THE INCIDENT GEOMETRY, made hostile: a remote long enough that the old
# msg[:200] cap would eat the entire fault clause.
LONG_URL = ("git@some-enterprise-git.internal.example-corporation.com:"
            "platform-infrastructure-group/securedchat-bus-mirror-frankfurt.git")
HOSTILE = (f"From {LONG_URL}\n"
           f" * branch            fleet-main-integration -> FETCH_HEAD\n"
           f"   abc1234..def5678  fleet-main-integration -> origin/fleet-main-integration\n"
           f"error: {FAULT}")


def main():
    print("[the defect: fault identity survives boilerplate of ANY length]")
    old = HOSTILE.strip().replace("\n", " ")[:200]
    check("negative control: OLD msg[:200] behavior really loses the fault",
          FAULT not in old, f"old warning still contained it: {old!r}")
    got = _summarize_git_failure(HOSTILE, None)
    check("THE FIX: fault clause survives the long-remote geometry",
          FAULT in got, f"got={got!r}")
    check("boilerplate is dropped, not merely appended-after",
          LONG_URL not in got and "FETCH_HEAD" not in got, f"got={got!r}")

    print("[truncation is never silent]")
    long_fault = "error: " + "x" * 400
    got = _summarize_git_failure(long_fault, None)
    check("over-limit output IS capped", len(got) < 400, f"len={len(got)}")
    check("...and the cap announces itself with the omitted count",
          "more chars]" in got, f"got={got!r}")
    check("under-limit output carries NO truncation marker",
          "more chars]" not in _summarize_git_failure("error: short", None))

    print("[nothing is swallowed]")
    check("empty output says so, does not render as blank",
          _summarize_git_failure("", "") == "no output captured")
    check("all-boilerplate output falls back to the lines, reports SOMETHING",
          _summarize_git_failure(f"From {LONG_URL}", None) != "no output captured"
          and _summarize_git_failure(f"From {LONG_URL}", None) != "")
    check("stdout is read when stderr is empty (rebase reports on stdout)",
          "CONFLICT" in _summarize_git_failure(None, "CONFLICT (content): merge conflict in chat.jsonl"))

    print("[_is_git_noise: both polarities]")
    check("'From <url>' is noise", _is_git_noise(f"From {LONG_URL}"))
    check("success ref line is noise", _is_git_noise("* branch  main -> FETCH_HEAD"))
    check("sha-range ref line is noise", _is_git_noise("abc1234..def5678  main -> origin/main"))
    check("'! [rejected]' ref line is a FAULT, kept",
          not _is_git_noise("! [rejected]        main -> main (non-fast-forward)"))
    check("error line with an arrow in it is a FAULT, kept",
          not _is_git_noise("error: mapping x -> y failed"))
    check("plain fault clause is not noise", not _is_git_noise(f"error: {FAULT}"))

    print()
    if FAILS:
        print(f"FAILED: {len(FAILS)} check(s): {FAILS}")
        return 1
    print("all checks pass")
    return 0


if __name__ == "__main__":
    sys.exit(main())
