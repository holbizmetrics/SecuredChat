"""test_presence_visibility.py — "did my write succeed" is not "can peers see me".

THE INCIDENT (2026-09-04, linux-claude-5534b575, measured not recalled):

    now                              1788508622   (07:57:02Z)
    my presence on origin            1788475179   ->  33,443s stale = 9h17m
    a peer's presence on origin      1788508552   ->  70s fresh

That box's presence sweeper had died in a container restart. Every remaining
heartbeat sat in a local clone 260 commits ahead of origin, and to every peer on
the bus the box read OFFLINE for over nine hours while its operator believed the
monitor was up.

WHY NO EXISTING TEST WOULD HAVE CAUGHT IT, which is the whole point of this file:
announce_presence pushes and _push_with_retry RAISES on exhaustion, so a "push
failure is reported" test passes today and always would have. No push failed.
NOTHING WAS BEATING AT ALL. A test built around the write path cannot see an
absent writer. So presence_visible_age asks the only question whose answer is
the same under every cause -- dead daemon, diverged clone, wrong branch, failed
push -- namely: what does the remote say about me?

THE THREE-WAY RETURN is the load-bearing part, and each arm is asserted here:
  float  seconds of staleness peers see
  inf    resolved the remote, no presence file: peers see us as never present
  None   CANNOT TELL. Not "fine". A caller that renders None as visible has
         turned absence of data into data -- the failure this whole file exists
         to catch, one level up.
"""
import json
import pathlib
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
from transport import GitBusTransport, FileBusTransport  # noqa: E402

FAILS = []


def check(name, cond):
    if not cond:
        FAILS.append(name)
    print(f"  [{'PASS' if cond else 'FAIL'}] {name}")


def git(repo, *args):
    return subprocess.run(["git", "-C", str(repo), *args],
                          capture_output=True, text=True, check=False)


def make_origin_and_clone(tmp):
    """A bare 'origin' plus a working clone, so remote-tracking refs are real."""
    root = pathlib.Path(tmp)
    origin = root / "origin.git"
    origin.mkdir()
    git(origin, "init", "--bare", "--initial-branch=main")
    seed = root / "seed"
    seed.mkdir()
    git(seed, "init", "--initial-branch=main")
    (seed / ".securedchat-bus").write_text("test\n")
    git(seed, "add", "-A")
    git(seed, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "init")
    git(seed, "remote", "add", "origin", str(origin))
    git(seed, "push", "-u", "origin", "main")
    clone = root / "clone"
    git(root, "clone", str(origin), str(clone))
    return origin, clone


def main():
    print("test_presence_visibility")

    with tempfile.TemporaryDirectory() as tmp:
        origin, clone = make_origin_and_clone(tmp)
        t = GitBusTransport(clone, "relay", "alice")

        # ---- arm 1: never beaten -> PRESENCE_NEVER, not 0 and not None ---- #
        age = t.presence_visible_age()
        check("CONTROL: with no beat ever, peers see us as NEVER present (inf)",
              age == t.PRESENCE_NEVER)

        # ---- arm 2: a real beat is visible on origin ---------------------- #
        t.announce_presence()
        age = t.presence_visible_age()
        check(f"THE CONTROL THAT MUST PASS: a pushed beat is visible on origin "
              f"(age={age if age is None else round(age, 1)}s)",
              age is not None and age != t.PRESENCE_NEVER and age < 60)

        # ---- arm 3: THE INCIDENT. Beat locally, never reach origin -------- #
        # Exactly the observed shape: the local tree is fresh and correct, the
        # commit exists, nothing errored -- and peers still cannot see it.
        before = t.presence_visible_age()
        pfile = t.presence_dir / "alice.json"
        pfile.write_text(json.dumps(
            {"identity": "alice", "ts": time.time(), "kind": "presence"}) + "\n")
        git(clone, "add", "-A")
        git(clone, "-c", "user.email=a@a", "-c", "user.name=a",
            "commit", "-m", "presence: relay alice (never pushed)")
        local = json.loads(pfile.read_text())["ts"]
        check("CONTROL: the LOCAL file really is fresh (so the arm is about "
              "visibility, not about a missing write)", time.time() - local < 5)
        time.sleep(1.1)
        after = t.presence_visible_age()
        check(f"THE CASE: an unpushed beat does NOT refresh what peers see "
              f"(origin age went {round(before or -1, 1)}s -> {round(after or -1, 1)}s, "
              f"i.e. it aged instead of resetting)",
              after is not None and before is not None and after > before)

        # ---- arm 4: a diverged clone cannot hide behind a local commit ---- #
        # Push a conflicting history to origin from a second clone so ours is
        # genuinely behind+ahead, the state the incident box was in.
        other = pathlib.Path(tmp) / "other"
        git(pathlib.Path(tmp), "clone", str(origin), str(other))
        (other / "unrelated.txt").write_text("x\n")
        git(other, "add", "-A")
        git(other, "-c", "user.email=b@b", "-c", "user.name=b", "commit", "-m", "peer work")
        git(other, "push", "origin", "main")
        div = t.presence_visible_age()
        check(f"a diverged clone still reports the ORIGIN's view, not its own "
              f"(age={round(div, 1) if isinstance(div, float) and div != t.PRESENCE_NEVER else div}s)",
              div is not None and div > 1.0)

    # ---- arm 5: "cannot tell" must be distinguishable from "fine" -------- #
    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / ".securedchat-bus").write_text("test\n")
        git(root, "init", "--initial-branch=main")
        (root / "f").write_text("x")
        git(root, "add", "-A")
        git(root, "-c", "user.email=t@t", "-c", "user.name=t", "commit", "-m", "i")
        t = GitBusTransport(root, "relay", "alice")
        check("DIRECTION: with no remote at all the answer is None (cannot tell), "
              "never 0.0 (which would read as 'freshly visible')",
              t.presence_visible_age() is None)

    with tempfile.TemporaryDirectory() as tmp:
        root = pathlib.Path(tmp)
        (root / ".securedchat-bus").write_text("test\n")
        t = FileBusTransport(root, "relay", "alice")
        check("CONTROL: a file-bus has no origin to be visible on -> None",
              t.presence_visible_age() is None)

    if FAILS:
        print(f"BATTERY: {len(FAILS)} FAILURE(S) -> {FAILS}")
        sys.exit(1)
    print("BATTERY: ALL GREEN. A local beat that never reaches origin is reported "
          "as stale, an absent one as never-present, and an unknowable one as "
          "unknown -- never as visible.")


if __name__ == "__main__":
    main()
