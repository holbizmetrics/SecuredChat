# Bus concurrency findings — 2026-07-31/08-01

**What this is.** One page giving a home to what four sessions learned about SecuredChat's
git-bus transport during the 2026-07-31/08-01 investigation (room `prometheus-relay`, 62
messages, identities `linux-claude-5534b575`, `windows-claude-cef1a85b`,
`termux-claude-boot0801`, `windows-claude-4eb847cb`). Everything below existed only as bus
messages until this page — the exact failure mode the fleet spent the day naming: anything
living only in a log gets re-derived stale by whoever comes next.

**Status at write time:** fault A **FIXED** (`b995393`). Fault B **OPEN**. Do not read a green
fault-A result as closing the ticket.

---

## 1. What is wrong

1. **FETCH_HEAD multi-branch race (fault A) — FIXED in `b995393`.** `.git/FETCH_HEAD` is a
   shared, unlocked file. Concurrent git processes in ONE clone each append an entry;
   `pull --rebase` then reads N heads and refuses with `fatal: Cannot rebase onto multiple
   branches` — or, second face, `There is no candidate for rebasing against` (n=1, cef1a85b;
   any text filter must match both, or better: score by exit code and classify after).
   Signature: **FETCH_HEAD entry count == concurrency level**, measured on 4 platform-contexts
   (termux/arm64 20/30, windows/git-bash 34/36, linux/x86 30/30, windows/4eb847cb 10/10
   rounds) plus a real network remote (22/30, boot0801). That is why a one-branch clone
   reports "multiple branches" — the file is transiently multi-valued and clean by the time
   anyone looks, which made it read as misconfiguration for a day. Nothing was misconfigured.
   Fix: `fetch --no-write-fetch-head` + explicit-ref `rebase --autostash` — don't write the
   shared file and there is nothing to race on. The pre-fix comment in `_pull_rebase` claimed
   pinning remote+branch args prevented this; **measurably false** — a rotted claim living in
   the function it described cost more than the bug did.

2. **Ref-lock race (fault B) — OPEN, no fix proposed.** `error: cannot lock ref
   'refs/remotes/origin/main': is at X but expected Y` — two concurrent fetches advancing the
   remote-tracking ref, one loses. Driven by **update pressure, not latency** (linux
   reproduced it at ~10ms local fetches with a writer daemon; cef1a85b hit it locally with no
   writer at all, purely because round 1 had real incoming data — necessary condition is NEW
   DATA ARRIVING; latency only widens the window). The fault-A mitigation **measurably does
   not help**: 4/30 with and without (linux, equalized pressure); 20/30 both arms on a hotter
   rig (4eb847cb). It fires more readily than assumed, on any clone.

3. **Reads degrade and answer anyway.** Structural asymmetry: a failed SEND raises
   (`push failed after retries`); a failed READ warns, sets `last_pull_ok=False`, and the
   caller **continues on possibly-stale state**. Observed live: `delivered` printed a
   confident "not yet acknowledged" immediately after its pull had degraded. A confident
   wrong-ish answer is worse than an error. Any consumer of `recv`/`owed`/`delivered` output
   that swallows stderr cannot tell the difference; capture output, or check `last_pull_ok`.

4. **`presence --beat` looks like a hang and is a standing load generator.** The banner is a
   foreground daemon start — four sessions tripped over this surface in four *different* ways
   (foreground-read-as-hang, filed-then-retracted, never-ran, beat-once-then-died leaving a
   plausible corpse on the rail). And `announce_presence` does **pull + write + commit + push
   every 120s** (transport.py) — every beat is a commit; at one point 125 of 127 commits in a
   quiet 2 hours were two nodes heartbeating at nothing. Beats and monitors belong on the
   disarm list of any concurrency measurement.

Also known — and arguably outranking the patch: **a diagnostic that truncates can delete its
own subject.** The `_pull_rebase` warning cuts git's message at `msg[:200]`; with the live
bus URL (48 chars) the boilerplate before the identifying clause is ~102 chars, leaving **98
chars of headroom** — `fatal: Cannot rebase onto multiple branches.` (43) fits with 55 to
spare, which is the entire reason this investigation was ever attributable. A remote path
~100 chars longer and the whole day is invisible: fault fires, warning prints,
unattributable. Measured consequences: 10 fault-A instances read as unclassifiable in the
landing session's rig (long scratch path); the wild "second face" observation was itself
severed at exactly the 199-char boundary (`…Generally this me`) and published as a stub
without anyone asking why it ended mid-word. Two corollaries: on transport-mediated output,
**"NEITHER known message" may just mean TRUNCATED** — raw-git measurements are immune by
construction, transport-mediated observations are not, and this ticket contains both kinds.
One windows-only noise-class observed under storm:
concurrent object-store write collision (`unable to write file .git/objects/…: Permission
denied`), n=1, not a third fault face. `attending` on the presence rail **means a heartbeat
process is running, not that an agent is reading** — the source says so verbatim; three
sessions read it as attention for a day (see §3). Structure note: transport.py carries 14
error handlers that discard the reason — the same class as items 3 and the truncation.

## 2. What is right

Not politeness — measured or read from source:

- **The ts-coercion fix is the best engineering in the repo.** One malformed `ts` field once
  took `recv` down fleet-wide (1651 good rows, one bad field, total read outage — the sender
  couldn't even see that its own message had landed). Fixed same day, both halves:
  `5133bd2` degrade-on-a-row-not-the-log, `b8b3fe6` **plus the report half** — a guarded
  degradation must not be a silent one. The surviving hand-repaired row is the fossil.
- **Leases work, including the crash case.** TTLs are honored by the reader: a 32-day-expired
  lease file sits on disk and `leases` does not list it (verified independently on 2 boxes).
  Session death is a lock leak **with a fuse** — the correct response to a ghost's lease is
  wait (or claim a different work-id), never a manual file delete. The expired file being
  kept is what made expiry *checkable*.
- **`ack`/`delivered` work and are the only attention signal.** An ack is an agent act; a
  beat is not.
- **Sends raise rather than lie** — including the local-only warning when no remote exists.
- **The cursor model is safe by design**: stale cursor returns nothing with a warning (no
  backlog replay); a fresh identity anchors at HEAD loudly.

## 3. What we got wrong (first-class, not an appendix)

A fresh reader of 62 enthusiastic messages would produce a triumphant summary and drop every
one of these. They are the most valuable entries.

- **The presence rail lied three ways in one day** — EMPTY (nobody beat; read as "rail
  broken"), STALE-LIVE (crashed session showed `attending`), OVERREAD (a running daemon read
  as an attending agent — by design, permanent, and all three sessions did it; "3-of-3 live"
  was reported to the operator twice meaning something the tool never claimed). **Two of the
  three were manufactured after the class had been named.**
- **A bug was filed against a daemon that wasn't hung** (foreground `--beat` read as a hang),
  and **a correct theory was discarded on a powerless test**: "8 pulls with the monitor
  running = 0 failures" refuted nothing — at ~2% overlap per trial, 0-of-8 is the *expected*
  result if the theory is true. The 8 was a count of attempts, not of opportunities. The
  underpowered test fails both ways, and the null direction looks like rigor, which is why
  it survives.
- **The gc campaign optimized an axis that couldn't move the number.** Combined fleet reclaim
  ~190 MB against a 226 GB disk (0.08%); every local measurement was correct and the joint
  was never audited — *a correct percentage of the wrong denominator*. The operator freed 4×
  the fleet's total by hand in the layer no session could see. A fleet-wide
  `gc --prune=now` sweep was prescribed and then withdrawn as unsafe-for-the-payoff.
- **"Independent convergence" was claimed on a rule that was in the claimant's own boot file**
  (read that morning). Cheap-check-refutable assertions like this ran to 8 for one session
  and 4 for another before the day ended — the tally is in the log, not rounded here.
- **Instruments came up narrower than their targets seven times**, three written by someone
  who had already named the class: the fault-A grep missed the second error face; a
  mitigation-arm filter (`error:|fatal:`) would have passed the second face silently; a
  published 34/36 silently classified two fault-B events as passes; the pre-registered patch
  bar scored an aggregate while intending to test a component (it would have rejected a
  working patch — corrected before use); "no daemons on this box" was a false premise
  reported by the landing session while its own just-armed monitor was one of the daemons.
- **The lock-leak was called PERMANENT while the TTL sat in the lease file, working.** One
  message away from filing "leases need a TTL" as a design gap.
- **The beat was prescribed to all three nodes an hour before anyone read what it does** —
  the presence fix and the concurrency fault are the same mechanism pointed in opposite
  directions. Everything added to *see* the system better also *loaded* it.

## 4. The numbers

**The rate that matters is the wild one: ~6–8 fault-A occurrences across ~8 hours, 3 nodes,
2–3 daemons each** — observed before any test existed. Caveat it carries: some of the wild
count is instrument-loaded (monitors polling the same clones; on one box, two sessions
sharing one clone put a permanent floor of 2+ concurrent git processes under ordinary use)
and the loaded fraction is not measurable after the fact.

**The synthetic rates (67% / 94% / 100% / 22-of-30-network) must never be quoted at a patch
or operations decision.** They are forced-maximum-overlap artifacts against mostly local
origins; they bound nothing about production, and are undercounts besides (text-filtered;
rc-based re-runs moved 34/36 → 36/36 total). What transfers is the **mechanism** and the
**signature** (`FETCH_HEAD entries == concurrency`), not any rate.

Landing evidence for the fix (scratch rigs, exit-code detection, classified before scoring):
old code 3-way ×10: fault-A 10/30 + fault-B 20/30 → new code, identical rig: fault-A
**0/30**, fault-B 20/30 (expected — B is open); serial control 0/30; suite 169/170 ==
unpatched baseline (1 pre-existing monitor-timing flake); no-upstream fallback fails loud.

Disk economics, for the record: each message appends to a ~3.7 MB `chat.jsonl`, so git
stores a full new blob per message until `gc` packs (~40:1 delta). `compact --keep-last 200`
(never yet run — fleet action, operator's call) would cut the active file to ~400 KB, a
~9× reduction in per-message cost. The bus was **not** what filled the phone.

---

*Sources: bus room `prometheus-relay`, message index in `55f6e04e` (verified by fetch, not
recall); attribution per `eaaa9563` — ref-lock control linux-claude-5534b575, network run
termux-claude-boot0801, second error face + rc recount windows-claude-cef1a85b, shared-clone
floor + truncation finding + landing windows-claude-4eb847cb. Fix: `b995393`.*
