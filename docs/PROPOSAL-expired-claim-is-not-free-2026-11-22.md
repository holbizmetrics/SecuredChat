# PROPOSAL: an expired claim is not a free claim (2026-11-22)

**Status:** proposal, nothing built. **Recommendation: HOLD until a collision is shown** (see § Prior art). Operator approved writing it (phone session termux-claude-3df44eb0,
2026-11-22). Idea taken from Frenemy 2 (github.com/noblehacks/frenemy, MIT),
`notes/WORKER-SAFEGUARDS.md`: *"Time alone never releases ownership. Do not run both old and replacement
workers."*

## The problem, in the code as it is (SecuredChat main, read 2026-11-22)

- `chat.py claim <work-id> --ttl S` writes `<room>/leases/<work-id>__<identity>.json`. Default TTL 1800 s.
- `transport.py _collect_leases`: a claim whose last renewal is older than its TTL is not alive. If no
  claim is alive, the work-id reads as **free**, and `acquire_lease` by any other identity returns
  `acquired`.
- Nothing tells the original holder. A session that ran out of **usage** (quota or plan limit) stops
  renewing without having died. When it comes back, it carries on with work that another session has
  since claimed. The original holder's next `claim` call returns `conflict` (exit 3), but only if it calls
  `claim` again before carrying on, and nothing makes it do that.
- `--ttl 0` (never expires) avoids the takeover, but then only the holder itself can release. A holder
  that really is dead holds the work forever.

Plan limits are an everyday stop condition in this fleet (2026-11-21: a 300-session run was put off to
the next day because of the usage limit), so "stopped renewing" usually means "paused", not "dead".

## The change (small)

1. **A third state for an expired claim: `stale`.** `leases` shows it as `stale <work-id> by <holder>,
   renewed <age> ago`, not as `free`.
2. **Claiming a stale work-id needs an explicit act.** Add `claim --take-over-stale --reason "<evidence
   the holder stopped>"`, which writes the reason into the new lease file. A plain `claim` on a stale id
   returns a new status, `stale-held`, and a new exit code. It does not return `acquired`.
3. **`abandon <work-id> --holder <id> --stopped --reason "..."`** for the `--ttl 0` case: a third party
   frees a dead holder's claim and leaves the reason on the bus.
4. **The returning holder finds out.** If a take-over happened, the original holder's next `claim` or
   `leases` call says so: *"your claim on X was taken over by Y at T: <reason>"*. It does not just say
   `conflict`.

Unchanged: first-claimer-wins at read time, one file per (work-id, identity), and the files stay out of
chat.jsonl.

## Not proposed

A Frenemy-style "currently working on job X" field in presence. On 2026-11-22 the phone session
believed it had evidence for that field (presence said "fresh" while send's warning said "hours ago").
The readings were in fact taken hours apart; the send warning was correct both times. No evidence for
it remains. Logged as a G17 recurrence in PCLA's gap ledger.

## Prior art on the bus (found before committing; it lowers this proposal's standing)

- **Expiry on time is a deliberate fuse, not an oversight.** On 2026-08-01, Linux reported that a crashed
  session "leaks" its claim. Windows `04ef0df0` measured the opposite from the lease files: "LEASES EXPIRE.
  THE GHOST IS NOT PERMANENT" (a 47,264-minute-old file is not listed as held). Linux later summed it up
  as "a lock WITH A FUSE" (`55f6e04e`). Any change has to keep that fuse. The `stale` state above keeps
  it: a stale claim can still be taken over, just not silently, so a crash does not lock the work away
  for good.
- **Standing view against building this early:** windows-claude-2dff9f70, `398d1b07`, 2026-09-18 (on a different
  PR): "a lease nobody expires is the A-6 anti-pattern in a new costume. Earn it on a demonstrated
  collision, not before." This proposal is not "nobody expires", but the same test applies: **no collision
  of this kind (a paused holder coming back to work that was taken over) has been shown.** I found none
  among the 1,907 messages in `prometheus-relay`. I searched by text pattern only, so that is a search
  result, not proof that none happened.
- **The behaviour-level rule already exists.** Dozens of end-of-session messages carry "please do not take
  over a lane I have claimed. If I have an open claim it still stands." That is Frenemy's rule, written
  as a request between sessions rather than as code.

**So the recommendation is to HOLD.** Build only once a real collision shows up. This file is the plan
for that day.

## Size, and whether it is worth it

Claims are rarely used: 35 lease/lease-release commits in the bus's whole history; the last were on
2026-11-18 (`operator-questions-linux`). So this guards a path that is rare today. It matters only if
claims become the way sessions split up work during unattended runs. **If that is not planned, the
right decision is to leave the code alone and keep this file as the record.**

## How we would know it works (test that can fail)

In `cli/test_chat.py`:
- A claims with TTL 0.05 s and waits until it expires. B's plain `claim` must NOT return `acquired`.
  **FAILS IF** it returns `acquired`, which is exactly today's behaviour, so this test is red on current
  main.
- B's `claim --take-over-stale --reason r` returns `acquired`, and the lease file contains `r`.
- A's next `claim` names B and `r`. **FAILS IF** A sees only a bare `conflict`.
- `abandon` without `--stopped` or without `--reason` is refused.
