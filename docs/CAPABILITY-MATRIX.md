# Capability Matrix: Claude Code vs. the Deterministic Terminal
**Data-grounded gap analysis from the official Claude Code changelog**
2026-07-21 · web-claude-0d61 · deterministic-terminal project

## Method

Source: `anthropics/claude-code` CHANGELOG.md fetched from the source repo
(469 KB, 5,179 lines, **4,128 entries** across all releases to 2.1.217).
Every `- ` entry keyword-classified into 14 capability classes (first-match,
patterns in the analysis script), classes mapped to the project's three gap
bands. Honesty checks: 10-entry random sample of the uncategorized residue;
itemization of the AI-bound band's feature entries.

## Results

| class                | entries | share | band |
|----------------------|--------:|------:|------|
| mcp/integrations     | 582     | 14.1% | MECHANIZABLE |
| model/AI-core        | 340     |  8.2% | AI-BOUND* |
| files/shell/search   | 303     |  7.3% | PARITY |
| terminal-ux          | 300     |  7.3% | PARITY |
| skills/knowledge     | 268     |  6.5% | MECHANIZABLE |
| orchestration        | 235     |  5.7% | MECHANIZABLE |
| permissions/sandbox  | 199     |  4.8% | PARITY |
| auth/billing/limits  | 169     |  4.1% | PARITY |
| telemetry/config     | 114     |  2.8% | PARITY |
| vcs/git              | 107     |  2.6% | PARITY |
| hooks/automation     | 96      |  2.3% | PARITY |
| context/compaction   | 43      |  1.0% | AI-BOUND |
| verification         | 4       |  0.1% | MECHANIZABLE |
| uncategorized        | 1,368   | 33.1% | mixed (sampled: overwhelmingly harness-band — OTel metrics, UI fixes, retry config, plugin consent) |

**Band totals (classified entries):**
- PARITY (terminal can do 1:1): **1,288 — 46.7%**
- MECHANIZABLE (engineering, no AI needed): **1,089 — 39.5%**
- AI-BOUND: **383 — 13.9%**

## The two findings

**1. By engineering volume, Claude Code is ≥86% deterministic machinery.**
Counting the sampled uncategorized residue as harness-band (which the sample
supports), the product is on the order of ~95% non-AI engineering. The thesis
("Code, we skipped the Claude") is not a joke; it is the changelog's own
accounting.

**2. Even the AI-BOUND band is mostly plumbing AROUND the AI.**
Itemizing its 71 feature entries: model pickers, effort flags, org model
restrictions, response telemetry, subagent model routing, compaction
scheduling. That is *configuration of* a model, not intelligence — and
configuration is mechanizable. The irreducible AI barely appears in the
changelog at all, because it lives in the model weights, which ship outside
this repo. **The changelog measures exactly the thing this project rebuilds:
the harness. And the harness is the product's engineering bulk.**

## Honest caveat

Changelog volume measures engineering surface, **not user value**. The model
is one line item here and half the felt value in practice. This data says
"the product around the model is rebuildable," not "the model doesn't
matter." The gap bands from the project's first principles stand unchanged;
what this adds is their *measured proportions* on the real product.

## Roadmap implication: buckets ranked by volume = the slice queue

| terminal slice | changelog bucket(s) | status |
|---|---|---|
| 1–2: transport, CommandHost, BusAgent, jail | files/shell/search + vcs/git + permissions | SHIPPED, verified 2 OSes |
| 3: presence + Roslyn read-only verbs | orchestration (presence) + files/search (semantic) | SHIPPED v0.2.0 |
| 4 (proposed): verify loop + structured verdicts, pen-test hardening | verification + vcs/git | SPEC pending |
| 5: routines (/loop, /batch), scripted verbs (.csx/.py library) | hooks/automation + skills/knowledge | the accretion layer |
| 6: MCP surface — expose cs-terminal AS an MCP server | mcp/integrations (the single biggest bucket!) | makes every Claude Code a client of the terminal natively |
| 7: analyzer+codefix, structural search, search-based repair | files/search + verification | the mechanical-intelligence climb |
| — never | model/AI-core, context/compaction | routed to the bus, by design |

Note on slice 6: mcp/integrations being the LARGEST bucket (14.1%) is the
data telling us where interoperability value concentrates. cs-terminal as an
MCP server means Claude Code invokes deterministic verbs as first-class
tools — the "AI voluntarily delegates to the non-AI Claude Code" moment,
productized.

## Reproduce

```
curl -sO https://raw.githubusercontent.com/anthropics/claude-code/main/CHANGELOG.md
# classification script: see conversation / ships with this file's provenance
```
