# SecuredChat Web Relay companion — v0.1.2

The localhost HTTP companion that bridges the browser extension
(`BrowserExtensions/Chrome Extensions/SecuredChat Web Bridge`) to this repo's `cli/chat.py`.
Localhost-only, token-authed, extension-origin-only.

## Why this lives here now

It previously existed only as unpacked directories beside the repos
(`SecuredChat-Companion-v0.1.0/`, `-v0.1.1/`), with the 0.1.2 change carried as a 44-line diff
inside a relay packet. A diff in a message is evidence, not a baseline: it cannot be deployed,
a later copy cannot be verified against it, and if the packet is lost the implementation goes
with it. Codex named that in its delta on extension commit `3b0b139`. This directory is the
durable version-controlled copy; `securedchat-companion-0.1.2.sha256` pins the same tree as a
content-addressed artifact so the two can be checked against each other.

**The home ruling is still the operator's.** `BRIDGE-DESIGN-2026-08-18.md` records the
companion's home as codex's/operator's call and notes this repo is the natural fit because it
IS transport. This branch acts on that note; it does not overrule it. Nothing is pushed and
nothing is merged.

## What 0.1.2 changes

`/v1/health` now returns an `instance` field: `secrets.token_hex(16)`, generated once per
process at import and never reused.

The extension binds its Mode-2 arming window to that exact value and disarms every site the
moment it changes. Before this, the extension derived a local "connection generation" from
`version|room|securedchat` — which cannot see a companion that restarts completely between two
polls and returns with identical fields. To the extension that looked like an uninterrupted
process, so an armed AUTO window survived a real restart. No client-side heuristic closes that
gap; the companion has to say who it is.

Consequence, deliberate: a companion older than 0.1.2 reports no instance id and therefore
**cannot be armed against at all**. An older companion is exactly the case where a restart is
invisible, so tolerating it would be tolerating the defect.

## Verify

    python -m unittest discover -s companion/tests -t .     # 17 tests
    sha256sum -c securedchat-companion-0.1.2.sha256          # against the packaged artifact

## Provenance

- v0.1.0, v0.1.1: built by chatgpt-codex, delivered as zips, unpacked beside the repos.
  v0.1.1 is preserved untouched at `D:/FromGitHubEtc/SecuredChat-Companion-v0.1.1/`.
- v0.1.2: this tree. Diff from v0.1.1 is 44 lines across `bridge.py` and its tests.
- Not started, not deployed, not installed. Only its unit battery has been run.
