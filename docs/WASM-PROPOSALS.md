---
title: Wasm proposals - what Wasmtime enables by default, what needs a flag, and where waboros stands
kind: reference
status: draft
updated: 2026-11-03
---

# Wasm proposals: Wasmtime's defaults and flags, and waboros

A note from the maintainer's discussion, for the orchestrator.

**The goal is feature parity with Wasmtime (maintainer).** Every proposal Wasmtime enables by
default is a proposal waboros implements on every target that claims it (D25), with the same spec
directories passing; proposals behind a Wasmtime flag follow, behind an option of ours. This table is
the parity checklist: a row is closed when its spec commands pass on `pyc` and both JITs at parity.

It measures the distance by Wasmtime's **actual defaults**, checked, rather than by the website's
`features.json` (our pinned copy, `docs/upstream/features.json`, is already behind: it lists
wide arithmetic as a flag, while Wasmtime 50 accepts it by default).

## How it was checked

Wasmtime CLI **50.0.1** (the version of the wasmtime-py oracle), one minimal module per proposal
(written as WAT, parsed by `wasm-tools` 1.263.0, which does not validate), then
`wasmtime compile x.wasm` **without flags**: accepted means enabled by default. Each refusal was
retried with `-W <proposal>=y`, and each default was confirmed by `-W <proposal>=n` making it fail.
2026-11-03. Reproduce with the same modules (below) whenever the oracle version changes.

## The table

| proposal | phase | in Wasm 3.0 | Wasmtime 50 | waboros (all three targets) | plan |
|---|---|---|---|---|---|
| multi-value, bulk memory, reference types, sign-ext, sat-float-to-int, mutable globals | 5 | (2.0) | default | yes (`partial` only from GC/exceptions tests in their files) | done |
| SIMD | 5 | (2.0) | default | yes | done (S0005) |
| multi-memory | 5 | yes | default | yes | done (D37) |
| extended-const | 5 | yes | default | yes | done |
| tail calls | 5 | yes | default | yes | done |
| typed function references | 5 | yes | default | yes | done |
| **memory64** | 5 | yes | **default** | no (8 719 spec commands) | phase 5 |
| **relaxed SIMD** | 5 | yes | **default** | no (79) | phase 5 |
| **exceptions** (`exnref`) | 5 | yes | **default** | tags validate only (110 + 9 core) | phase 5 |
| **GC** | 5 | yes | **default** | no (798 + most of the 91 core xfails) | phase 5, last |
| **threads** (shared memory, atomics, wait/notify) | 4 | no | **default** (`-W threads=n` refuses it) | no | late (ROADMAP item 8) |
| **wide arithmetic** (`i64.add128`, `i64.mul_wide_*`) | 4 | no | **default** (`=n` refuses it; the website still says flag) | no | ROADMAP, after GC (added 2026-11-03) |
| branch hinting | 5 | no | accepted by default (a custom section; `=n` still accepts the module) | accepted (an unknown custom section is skipped) | optional: use the hints for block layout |
| custom page sizes | 3 | no | **flag** (`-W custom-page-sizes=y`) | no | late (ROADMAP item 8) |
| stack switching | 3 | no | **flag** (`-W stack-switching=y`) | no | not planned |
| shared-everything threads | 1 | no | flag exists, a shared global still refused | no | not planned |
| compact imports | 4 | no | flag exists; not tested here | no | trivial when needed (binary format only) |

Also in Wasm 3.0 but not new instructions: the **deterministic profile** (canonical NaNs,
deterministic relaxed SIMD; Wasmtime has `-W nan-canonicalization` and
`-W relaxed-simd-deterministic`, both off by default), custom annotation syntax (text format only)
and JS string builtins (JavaScript only).

## What follows for waboros

* **Parity with Wasmtime's defaults** needs **six** more proposals: the four of Wasm 3.0
  (memory64, relaxed SIMD, exceptions, GC) plus **threads** and **wide arithmetic**. All six belong
  in ROADMAP as parity items. ROADMAP has no item for wide arithmetic yet; it is small (128-bit
  add/sub and widening multiply on register pairs, `mul`/`adc` on x86-64, `umulh`/`adds`/`adc` on
  aarch64) and could join relaxed SIMD's step. Threads are the hardest for `pyc` (shared memory
  needs real synchronisation, ROADMAP open question 8) and stay last, but they are on the path, not
  out of scope.
* **Parity with Wasmtime's flags** comes after: custom page sizes, stack switching,
  shared-everything threads, compact imports, each behind an option of ours as Wasmtime has it
  behind `-W`. None is needed by the toolchains' default output, so they follow the defaults.
* **ROADMAP order (maintainer): what ROADMAP does not list yet goes to the end of the list.** The
  order of the opcode stages stays as it is (memory64, relaxed SIMD, exceptions, GC) and is then
  extended, in this order:
  1. **wide arithmetic** (Wasmtime default; not in ROADMAP today);
  2. **threads** (Wasmtime default; today only in "Order of the wasm features", item 8, and open
     question 8);
  3. the flag-only proposals, each behind an option: **custom page sizes** (already in item 8),
     **compact imports**, **stack switching**, **shared-everything threads**.

  Nothing already planned moves; the new items only extend the list.
* Branch hinting needs nothing for correctness; using the hints is an optimisation for later
  (block layout, after the code-placement work).
* When the deterministic profile matters (reproducible results across hosts), it maps to two
  options we do not have yet: NaN canonicalisation (S0005 Q5, deferred) and deterministic relaxed
  SIMD (its spec should offer it from the start).
* Refresh `docs/upstream/features.json` from the website, and keep this table in step with the
  oracle's version: the website lags Wasmtime's own defaults.

## The probe modules

```wat
threads:            (module (memory 1 1 shared) (func (result i32) i32.const 0 i32.atomic.load))
memory64:           (module (memory i64 1) (func (result i32) i64.const 0 i32.load))
gc:                 (module (type $s (struct (field i32))) (func (result (ref $s)) i32.const 1 struct.new $s))
exceptions:         (module (tag $t) (func throw $t))
relaxed-simd:       (module (func (param v128) (result v128) local.get 0 i32x4.relaxed_trunc_f32x4_s))
tail-call:          (module (func $f return_call $f))
function-references:(module (type $t (func)) (func (param (ref $t)) local.get 0 call_ref $t))
extended-const:     (module (global i32 (i32.add (i32.const 1) (i32.const 2))))
multi-memory:       (module (memory 1) (memory 1))
simd:               (module (func (result v128) v128.const i64x2 0 0))
custom-page-sizes:  (module (memory 1 (pagesize 1)))
wide-arithmetic:    (module (func (param i64 i64 i64 i64) (result i64 i64)
                      local.get 0 local.get 1 local.get 2 local.get 3 i64.add128))
stack-switching:    (module (type $f (func)) (type $c (cont $f)) (func $g) (elem declare func $g)
                      (func (result (ref $c)) ref.func $g cont.new $c))
branch-hinting:     (module (func (param i32) block local.get 0
                      (@metadata.code.branch_hint "\01") br_if 0 end))
shared-everything:  (module (global (shared i32) (i32.const 0)))
```
