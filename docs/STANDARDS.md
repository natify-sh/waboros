---
title: Documentation standards
kind: guide
status: current
updated: 2026-10-30
---

# Documentation standards

How documents in this repository are written, labelled and checked. `python3 tools/check_docs.py`
enforces the mechanical parts and must pass before a commit.

## Language and style
* English for code, comments, commits and documents (the maintainer talks Ukrainian).
* Line width about 100. Short sentences; numbers come with their conditions.
* A document that contradicts the code is a bug: fix the document or the code in the same commit.
* Bump `updated` whenever the content of a document changes.

## Front matter
Every `.md` under `docs/` (except `docs/templates/`), plus `BRIEF.md` and `README.md`, starts with a
YAML block between two `---` lines. `CLAUDE.md` is read by agents as plain text and has none.

| field | applies to | value |
|---|---|---|
| `title` | all | string |
| `kind` | all | `guide` (how to work), `reference` (how it is built), `spec`, `log` (entries, newest last), `index` |
| `status` | non-spec | `current`, `draft`, `outdated` |
| `updated` | all | `YYYY-MM-DD` |
| `id` | spec | `S0001`..., equals the filename prefix, unique |
| `status` | spec | `draft`, `approved`, `implemented`, `superseded` |
| `superseded_by` | spec | an S id; required if and only if status is `superseded` |
| `related` | spec | list of D/R/S ids, e.g. `[D15, R3, S0002]` |
| `owner` | spec | string |

Checker's YAML subset: one `key: value` per line; values are plain strings (no quotes needed) or
flat lists `[a, b]`; no nesting, no multi-line values. Templates in `docs/templates/` are not
checked (they hold placeholders).

## IDs
Decisions `D17` (DECISIONS.md, heading `### Dn.`), risks `R3` (RISKS.md, heading `## Rn.`), specs
`S0002` (`docs/specs/S0002-<slug>.md`). Mention an ID as written; link the file on first mention in a
document. The checker fails on an ID that does not exist.

## Specs
A spec in `docs/specs/` is required, and `approved`, before implementing a contract: the context
ABI, the JIT artifact format, the Target interface, the IR (HIR/LIR), trap semantics, the internal
engine interface of the spec runner. Lifecycle: `draft` -> `approved` -> `implemented` ->
`superseded` (set `superseded_by`). One file per component; template `docs/templates/spec.md`;
index `docs/specs/README.md` (update it with every spec). ARCHITECTURE.md and other reference
documents give the overview and link to specs; they do not repeat exact signatures or encodings.
A change to an implemented spec adds a dated line to its "Changes" section.

## Decisions (DECISIONS.md)
```
### D19. Title
Status: accepted · Date: YYYY-MM-DD · Supersedes: — · Evidence: <BENCHMARKS.md section / test / —>
Free text: what, why, what was rejected.
```
Status: `accepted`, `superseded by Dn`, `rejected`. Never renumber; to change a decision add a new
one with `Supersedes: Dn` and mark the old one `superseded by Dm`.

## Other entries (new ones; old ones are not rewritten)
* Benchmark (BENCHMARKS.md): date, commit, machine (CPU, cores), Python version, exact command,
  result, conditions. Template `docs/templates/benchmark.md`.
* Lesson (LESSONS.md): what happened, symptom, cause, rule, where found.
* Risk (RISKS.md): likelihood, impact, mitigation, owner spec.

## Which document to update
| change | update |
|---|---|
| a contract (ABI, artifact, Target, IR, traps, engine interface) | its spec first, then code; version bump rules in ARTIFACT.md |
| a choice between designs | DECISIONS.md |
| a measured speed or size | BENCHMARKS.md |
| a trap found on the way | LESSONS.md |
| a new danger or its answer | RISKS.md |
| overview of how things fit | ARCHITECTURE.md, TARGETS.md |
| tests, expected failures | TESTING.md |
| a feature's state on a target (claims, xfail, a step of a proposal) | FEATURES.md: the table via `tools/features.py` **and** the hand-written notes below it; TARGETS.md |
| order of work | ROADMAP.md |

## Steps, briefs and commits
Refer to dates, steps, decisions or CI run IDs, never to our own commit hashes: history may be
rewritten (`tools/check_docs.py` flags them).

Step briefs and reports are not stored as files. Each step is one commit; the message carries the
goal, the acceptance commands and the numbers before and after:
```
<area>: <what changed, imperative, <= 72 chars>

Goal: <one sentence, as a result a command shows>
Why: <context, only if not obvious>
Acceptance:
  <exact command>  -> <result>
Numbers: <metric> <before> -> <after>   (or "n/a")
Docs: <D/R/S ids and files updated, or "none">
```
