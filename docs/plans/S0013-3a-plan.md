---
title: S0013 step 3a implementation plan
kind: reference
status: outdated
updated: 2026-11-15
---

> The plan the jit-engineer wrote and the orchestrator approved for S0013 step 3a; done 2026-11-11. What was built and decided is in S0013 Changes (3a.1-3a.4) and D56; this file is the record of the plan, not a description of the code.

# S0013 step 3a: implementation plan (safepoints for every module, both JITs)

Status: PLAN, for the orchestrator's approval. No file of the repository was changed.
Read: S0013 (safepoints, roots, context ABI, artifact, test plan, steps, Q3/Q5/Q9/Q13, Changes of
1b), ARTIFACT.md parts 2-3, S0003, S0004, RISKS R2/R3/R6, LESSONS 1-9, 24, 31, 80, 93, 94, 98,
D39 and its amendments; the code: `jit/common/{backend,compiler,context,instance,target,simd}.py`,
`x86_64/{backend,abi,__init__}.py`, `aarch64/{backend,abi,__init__}.py`, `frontend/validate.py`
(masks), `tools/jit_dump.py`, `tests/jit/test_abi*.py`, `.github/workflows/ci.yml`.

## 0. What exists and what the code tells us

* Every machine `call` the JIT emits is in one of these places (checked by grep):
  x86-64 `Backend.call_direct`, `_call_record` (from `call_record`, `call_indirect`,
  `call_ref`), `call_host` (also reached from `throw`'s refill path), `host_thunk`; aarch64
  `call_direct` (`bl`), `call_host` (`blr x16`), `_enter_record` (from `call_record`,
  `_call_x15`), `host_thunk` (`abi.host_thunk`). Trampolines: entry, bridge, `tail_switch`
  (and the fault handler, which calls libc only and is never a wasm frame).
* **`throw` is a safepoint the spec's list does not name**: its cold refill path is a bridge
  call (`call_host(f, EXN_REFILL, base + n)`), so a walk can start there. It is recorded
  through `call_host` automatically; the core gives it shift 0 and a map over the payload.
* The core's operand stack `st` has **one entry per slot** (a v128 is two entries, D41): slot of
  stack entry i is `L + i`. The validator's `mod["refmasks"]` bit i is a **value position**
  (v128 = one position). The two numberings differ as soon as a v128 sits below a reference
  (open question A).
* Frames: x86-64 function `push` pins, `sub rsp, 8` if `len(pins)` is even; record call adds
  `push r15; sub rsp, 8`; thunk `sub rsp, 8`. aarch64 function `stp x29, x30, [sp, #-size]!`
  (`size = 16 + ceil(pins/2)*16`, record at the bottom), pins above; record call adds
  `stp x28, xzr, [sp, #-16]!`; thunk a 16-byte frame record. `tail_switch` restore frame:
  x86 `push r15` then `call`; aarch64 `stp x28, x30, [sp, #-16]!` then `blr`: in both the saved
  context at `[sp]`, the real return address at `[sp + 8]`.
* aarch64 recompiles a function in far mode after `rewind(pos)`: anything recorded per site must
  be rewound too (a trap for the implementation: listed in 3a.1).

## 1. Site records per backend (the D39 amendment)

**Form of the output.** Each backend gets an attribute `sites: list[tuple[int, int, int]]`
(`ret`, `ret_at`, `up`), appended by every method that emits a call, in code order. No method
signature changes, no instruction changes: the amendment is "a call method appends one record"
(alternative: return the record; see open question H). `common/backend.py` documents the field.
aarch64 `rewind(pos)` drops records with `ret > pos`.

* `ret` = `len(a.buf)` right after the `call`/`bl`/`blr`: the return address's code offset.
* `ret_at` = bytes from the stack pointer at the call (before x86's `call` pushes) to the slot
  holding **this frame's own** return address.
* `up` = bytes from that stack pointer to the caller's stack pointer at its own call.

| site | x86-64 (SysV = Win64: wasm code is the same, D23) | aarch64 (AAPCS64 = Apple = Win-arm64, S0004) |
|---|---|---|
| `call_direct`, `call_host` (incl. `throw` refill) | `ret_at = 8·(npins + pad)`, `up = ret_at + 8` | `ret_at = 8`, `up = f.size` |
| `call_record`, `call_indirect`, `call_ref` | `ret_at = 8·(npins + pad) + 16`, `up = ret_at + 8` | `ret_at = 24`, `up = f.size + 16` |
| `host_thunk` | `ret_at = 8`, `up = 16` | `ret_at = 8`, `up = 16` |
| `tail_switch` restore (trampoline table, §4) | `ret_at = 8`, `up = 16` | `ret_at = 8`, `up = 16` |

`pad = 1 if len(pins) is even` (x86 `Frame.pad`), `f.size` from aarch64's `Frame`. The
numbers come from the `Frame` the method already has; they are computed in Python only.
Calling conventions do not enter: wasm code and its frames are identical under every host
convention of an architecture; only the trampolines differ (§3).

**Evidence that code bytes stay identical (D35):** the change adds a list append after the
instruction is emitted and no `Asm` call; proved by the identity procedure of §9 (all spec
modules + CoreMark, both targets, guard and explicit, column "code digest" equal before/after),
and by `uv run pytest tests/jit/test_compiler*.py` (byte-level tests of the backends).

Budget: x86-64 ~12 lines, aarch64 ~14 (incl. `rewind`), `backend.py` ~3.

## 2. The stack-map builder (core)

**Where:** a new `src/waboros/targets/jit/common/gc.py` (the module the spec names; at this step
it holds only the stack-map part: build, encode, decode), called from `common/compiler.py`.

**Inputs per function** (`_Func`): `ltypes` and `slot[]` (locals, v128 = 2 slots), `L`, the
validator's `mod["refmasks"][k]` (pc -> mask), the reference predicate of the front end (the
validator's `_root`: any- or extern-hierarchy; at 3a only extern types reach the JIT because GC
modules are refused), and per site: the instruction index `pc`, the stack height `h0` below
the arguments, `nargs + nimms` for a service, the shift.

**Per site** the core records `(shift, map)`:
* wasm calls (`call`, `call_indirect`, `call_ref`; `call_record` included): `shift = L + h0`,
  `nslots = L + h0` (arguments belong to the callee's frame as its parameters);
* services (`call_host` of a libcall): `shift = 0`, `nslots = L + h0 + nargs + nimms`;
* `throw` refill: `shift = 0`, `nslots = L + h0 + n + 1`;
* host thunk: `shift = 0`, map 0 (its frame is the host function's arguments, read by the host
  before anything else).
* `bits = (locals_mask | operand_slot_mask << L) & ((1 << nslots) - 1)`; `locals_mask` is
  computed once per function (every reference local at every site until step 7); the
  operand mask comes from `refmasks[k][pc]` (in slot numbering, open question A).
* Fast path: `mod["refmasks"] == []` (no extern/any type declared anywhere: almost every module)
  means every map is 0; the builder does nothing but append `(shift, 0)`.
* Pairing with the backend: `_Func` keeps its own list; after the function,
  `assert len(b.sites) - before == len(func_sites)` (catches a call method that forgot its
  record, a far-mode rewind that kept stale records, a core site without a call).
* Assertions (invariants I1/I2 in code): no reference local is pinned
  (`not locals_mask_of_pins`); the mask has no bit at or above the operand height
  (`mask >> len(st) == 0`, catches validator/core drift); with masks on, every site's `pc`
  has a mask (KeyError otherwise).

**Output:** `Compiled.stackmaps: bytes | None` (new last field, default `None`), the
`STACKMAPS` payload of §5, encoded at the end of `compile_module` (sites sorted by `ret`,
already in code order; thunks after functions). Identical maps are shared in the pool.
`gc.decode(bytes) -> dict[ret, (shift, ret_at, up, nslots, bits)]` is used by the walker and the
round-trip test, so the section format is exercised now, not only written down.

**Compiler hooks:** `run()` iterates `for pc, ins in enumerate(self.insns)` and passes `pc` to
`call`, `call_indirect`, `call_ref`, `service`, `throw` (no per-instruction attribute store);
`compile_module` adds the thunk sites and encodes.

**Compile-time cost and its measurement:** reference-free modules pay one tuple append per site
and ~0.3 µs of `struct.pack` per site at the end (CoreMark: 108 sites, ~40 µs of a ~50 ms
compile). Measured as before (BENCHMARKS "Compile time"): scratch `ctime.py`, the standard set
(CoreMark, `hash`, `mono`, `zip`), median of 10 compiles per process, `taskset -c 2`, 10 rounds
alternated before/after from the two trees, `jit-x86_64` and `jit-aarch64` (cross-compiled),
guard and explicit. Bound: +3 %.

Budget: core ~120 (`gc.py` ~70, `compiler.py` ~40, `context.py` ~3, `frontend` ~5 if A(a)).

## 3. The exit record (context word 14)

**Store side:** `_StoreStacks.exit = (c_uint64 * 3)()` = `{sp, pc, frame}`, zero at creation;
`Instance.__init__` writes `ctx[EXIT_RECORD] = addressof(st.exit)` in every context (as words
10-13), so a record call into any context of the store finds it (LESSONS 31). `context.py`:
`EXIT_RECORD = 14`, word 15 stays reserved.

**Bridge (writes, then clears).** At every host call, before the host is called:

* x86-64 SysV and Win64, right after the saved pool registers are pushed (`r8`, `r9` are in the
  saved set of both): `mov r8, [r15+112]; lea r9, [rsp+8·n+8]; mov [r8], r9;
  mov r9, [rsp+8·n]; mov [r8+8], r9; mov [r8+16], r12` (`n` = 6 SysV, 4 Win64: `sp` is the
  caller's rsp before its `call` pushed, `pc` the return address, `frame` r12). After the host
  returns, before `mov eax, [status]`: `mov rcx, [r15+112]; mov qword [rcx], 0` (r15 is
  callee-saved in both conventions). 6 + 2 instructions, movs and a lea only: no flags (L1), no
  alignment change (L3), rax/rdx (word, slots) untouched.
* aarch64 (one bridge for every OS), before its frame record: `ldr x10, [x28, #112];
  mov x11, sp; stp x11, x30, [x10]; str x27, [x10, #16]` (x30 still holds the return address;
  x9-x15 are scratch at every call site). After `blr x16`: `ldr x10, [x28, #112];
  str xzr, [x10]`. 4 + 2 instructions.

**Entry (saves and restores, nested entries, L4).** Four more words on the native stack:
the enclosing exit record `{sp, pc, frame}` and the frame it was given.

* x86-64: `sub rsp, 8` becomes `sub rsp, 40` (SysV 8+48+40 = 96, Win64 8+64+40 = 112: both
  0 mod 16, L3); the block is at `[rsp+0..32)` at the `call`, the same offset in both
  conventions. After `mov r15, ctx; mov r12, frame`: `mov rax, [r15+112]`, three
  `mov rcx, [rax+8i]; mov [rsp+8i], rcx`, `mov [rsp+24], r12` (rax, rcx volatile in both
  conventions; the code pointer stays in rdx/r8). After `call code`, **before** `mov eax, eax;
  test; je` (nothing between a flags producer and its consumer, L1): `mov rcx, [r15+112]` and
  three `mov rdx, [rsp+8i]; mov [rcx+8i], rdx` (eax untouched; r15 is preserved by wasm code and
  by `tail_switch`). 8 + 7 instructions; `add rsp, 40` at the end.
* aarch64: `stp x29, x30, [sp, #-128]!` (was −96), x19-x28 unchanged at 16..96, the block at
  `[sp+96..128)`: after `mov x28, x0; mov x27, x1`: `ldr x9, [x28, #112]; ldp x10, x11, [x9];
  ldr x12, [x9, #16]; stp x10, x11, [sp, #96]; stp x12, x27, [sp, #112]`. After `blr x2`
  (before `cbz w0`): `ldr x9, [x28, #112]; ldp x10, x11, [sp, #96]; ldr x12, [sp, #112];
  stp x10, x11, [x9]; str x12, [x9, #16]`. 5 + 5; sp 16-aligned throughout; `ldp ..., #128`
  at the end.

Why it works for nesting: an outermost entry saves `sp = 0` (the bridge clears after every
host call, so outside any host call the record reads 0); a nested entry saves the record of the
host call it is nested in; its inner host calls overwrite and clear the record; when it returns
the record is put back, so the enclosing host function, and any walk from a later nested entry,
see the right one.

Cost: 8 instructions per host call, 15 per entry on x86-64 (10 + 6 on aarch64), against
>= 0.3 µs of ctypes per crossing (L9). The spec says "about five" and "about eight": the entry
is 15 because it restores as well as saves; it can be shortened (movdqu) only with a
measurement, which the plan does not expect to need.

**ABI v1 amended without a bump (Q5, maintainer 2026-11-06).** The words' positions are
unchanged, word 14 was reserved and is now defined, the trampolines are the runtime's own
(process-wide, not artifact code), and module code does not change (identity, §9), so code
compiled before runs unchanged with the new trampolines; no artifact file has been written
(ARTIFACT.md part 2 is fixed in phase 5). The artifact rule's test, as Q5 restates it: **a
compiled module without its site table is refused at instantiation** (§7). What "an old artifact
still runs" means here is covered by the identity lists: an old module's code is the new code,
and the whole suite runs it with the new trampolines and the walk check on.

## 4. The trampolines' table

`Trampolines` (in `instance.py`) gains `entry_ret` (address of the entry's return point, after
`call code` / `blr x2`), `restore` (`tail_switch`'s `restore` label) and `saved` (offset of the
saved block from the stack pointer at the entry's call: x86-64 0, aarch64 96). Each
`abi.assemble()` appends the two label offsets to its offsets list (entry, bridge, sp stub,
tail_switch, entry_ret, restore; existing unpacking in tests keeps working) and exports
`ENTRY_SAVED`; `load_trampolines(code, offsets, saved)`.

The walker's rules from the table:
* `pc == restore`: a pseudo-site `{shift 0, ret_at 8, up 16, no map}` (both architectures):
  steps over the saved context to the real return address; the base does not change (the
  tail-called function runs on the frame base of the one it replaced).
* `pc == entry_ret`: end of a segment; at `sp + saved` the words `{sp, pc, frame}` of the
  enclosing exit record and the entry's frame. The base the walk computed for the last frame
  must equal that frame (and the runtime's own record, §6). A saved `sp` of 0 ends the walk.
* The bridge needs no row: it is never a frame of the walk; the exit record describes the frame
  that called it. The `hop` path of `tail_switch` leaves no frame.

Budget: x86-64 abi ~20, aarch64 abi ~16, the two `__init__` ~2 each, `instance.py` ~8.

## 5. The `STACKMAPS` section (id 15)

Little-endian, as the spec's sketch, with the two details it leaves open fixed:

```
u32 nsites
nsites × { u32 ret; u32 shift; u16 ret_at; u16 up; u32 map }   # sorted by ret, 16 bytes
u32 nmaps
nmaps × { u32 nslots; ceil(nslots/32) × u32 words }           # bit i of the map = slot i
```

`map` = 0 means no reference slot; map k >= 1 is the k-th pool entry (1-based). Value ranges:
`ret_at`/`up` <= 160 bytes on both architectures (u16 holds them; asserted in `encode`).
ARTIFACT.md part 2 gets the row and this layout; the loader rule "refused without the section"
goes in as the spec states. Not in it at this step: the per-function reference-local bitmap
that step 7's nulling needs (open question C).

## 6. Walker and check mode (runtime)

**Where:** a new `src/waboros/targets/jit/common/walk.py` (~120 lines; the GC collector of step
5 reuses it), or inside `instance.py` (open question D).

**Registry:** lazily, on the first walk of a store: `sorted((code.addr, code.addr + len, inst))`
over `store.instances` (instances are only appended; cached by their count), and per instance
the decoded site dict (`gc.decode(c.stackmaps)`, built on first use). Nothing is built at
instantiation beyond keeping `stackmaps` on the instance: zero cost when the check is off.

**`frames(store, tramp)`** (a generator, also used by tests): start at the exit record
`(sp, pc, frame)`; for each frame: find the code range of `pc` (bisect), its site (dict);
yield `(inst, site, sp, base)`; step: `pc' = [sp + ret_at]`, `sp' = sp + up`,
`base' = base − 8·shift(site(pc'))`; `pc' == restore`: pseudo-site; `pc' == entry_ret`: end of
segment as in §4. Errors (each a `RuntimeError` naming the address and segment, never a
guess): `pc` in no range, no site at that `pc`, a base mismatch at an entry, `sp` not
increasing, `sp` beyond the stack top the runtime saw at the outermost entry (kept as
`st.ntop` where `_nstack_limit` already reads it: one assignment per outermost entry), more
segments than active entries. Every `pc` is validated before the next read, so a wrong
`ret_at` stops the walk at the first unknown address instead of reading far.

**Check mode** `WABOROS_JIT_WALK_CHECK=1`, read when an instance is created: the instance then
uses **checked** variants of its host callbacks (`_host`'s `callback`) and of the store's
refill service, so the hot path of a normal run is the same code as today (no flag test per
call). A checked callback, inside its existing `try` (a failure is a host error with the
`RuntimeError` pending, so the spec command fails, D34), before `serve`:
1. the exit record's `sp` is non-zero and `frame <= slots` (the bridge wrote it for this call);
2. walks the whole stack of the store; the number of segments equals `st.active`;
3. each segment's computed base equals the entry's saved frame **and** the runtime's own
   record: the outermost is `st.vstack.addr`, a nested one is the `st.vsp` an enclosing checked
   callback set (`st.walk_vsp`, a list kept only by checked callbacks);
4. after the last segment the saved `sp` is 0;
5. every marked slot holds 0 or a live externref handle (`<= len(st.externs)`): I3 on the
   whole suite.
Then it pushes its `slots + 8n` on `st.walk_vsp` and pops it in `finally`.

Cost when off: zero instructions in machine code beyond §3; in Python, `Instance.__init__`
reads one environment variable (or a module constant) and `_nstack_limit` stores one int.

Budget: `walk.py` ~120, `instance.py` ~60 (store words, ctx[14], checked callbacks, refill,
`ntop`, `stackmaps` kept, refusal), `target.py` 0-3. Runtime total ~190.

## 7. The refusal test

`JitInstance.__init__` (or `JitTarget.instantiate`) raises when `c.stackmaps is None`:
"compiled module has no stack-map table (STACKMAPS); recompile it" (exception type: open
question I). Test: `dataclasses.replace(target.compile(mod), stackmaps=None)` → instantiate
raises, nothing registered in the store; the same module with its table instantiates. The base
`Instance` (hand-assembled code in `tests/jit/test_abi*.py`) takes an optional `stackmaps`;
without one it is not walkable, and the walk reports its `pc` as unknown (those tests do not run
in check mode; open question G).

## 8. Tests and mutations

`tests/jit/test_walk.py` (~150 lines), each case on every JIT the machine has (QEMU: under
`tools/run_arm64.py`), check mode set through `monkeypatch.setenv` before instantiation:
* builder: exact maps on hand-written modules: an externref parameter and local at a call; an
  externref operand below a `call` (marked) and as an argument (not marked: callee's
  parameter); `table.grow` with an externref init value (service: the argument slot marked); a
  v128 local and a v128 operand below a reference (slot numbering, A); funcref never marked;
  `throw` refill site over an externref payload; a module without references: all maps 0.
* format: `decode(encode(x)) == x`, pool sharing, the u16 bounds.
* I1: a module with heavily used externref locals pins none of them and marks all.
* walk, frame counts (a host function returns `len(list(frames(...)))`): a direct chain of
  5; `call_indirect` and `call_ref` into another instance; a host import (thunk); a host import
  reached by `return_call` (thunk jumped to); a cross-instance `return_call` (restore frame,
  then a host call in the callee); `memory.grow` under frames; a `try_table` frame; nested
  entries 50 deep; two sequential nested entries inside one host call (catches "entry not
  restoring"); an outer call after earlier host calls (catches "bridge not clearing").
* refusal (§7).
* the walk check over `tests/jit/test_tail.py` and `test_exceptions.py` too (one parametrised
  run with the env set).

**Mutations** (scratch driver, not committed, LESSONS 80/93/94): each mutant applied to a
`git archive` copy of the tree, run in a subprocess with `start_new_session=True`, process
group killed on timeout, a crash or hang counted as caught; the runner is `test_walk.py` plus
the spec suite subset `core/call*`, `core/return_call*`, `exceptions/` in check mode. On
x86-64 natively and aarch64 under QEMU:
* `ret_at` +8, then `up` +8, for each of: direct, record, host thunk, restore (8 per arch);
* bridge does not write the exit record; bridge does not clear `sp`; entry does not restore;
  entry saves a wrong frame;
* a missing map bit (a local; an operand below a call; a service argument): caught by the
  exact-map builder tests (nothing moves yet, so the check mode cannot see it; open question B);
* `choose_pins` admits externref: caught by the I1 assertion/test.

## 9. Measurements and identity

**Identity (D35, LESSONS 98).** `git archive HEAD src tests tools poc/in | tar -x -C $BEFORE`,
the spec cache linked into it; `cd $BEFORE && python tools/jit_dump.py ...` and the same in the
working tree, for `jit-x86_64` and `jit-aarch64`, `WABOROS_JIT_BOUNDS=guard` and `explicit`
(8 lists). `jit_dump.py` gains a 4th column, the SHA-256 of `c.stackmaps` (the existing digest
does not include it). Pass: columns 1-3 equal line for line; every compiled line of the "after"
lists has the 4th column and no "before" line has it (proves which tree ran); the summary line
prints the number of modules with at least one non-zero map (> 0, proves the maps are real).
Also: the `jit-aarch64` lists from this x86 host equal the CI arm64 host's (CI already diffs
the whole line).

**Timings** (x86-64, this VM, `taskset -c 2`, the two trees alternated in one session, medians
and ranges in BENCHMARKS.md):
* host call: a wasm loop calling an imported `lambda a: [a[0]]` 10⁶ times, best of 5 per round,
  9 rounds; a libcall loop (`memory.grow 0`) 10⁶ times;
* entry: Python calling an exported `nop` 10⁵ times, 9 rounds;
* CoreMark `tools/run_coremark_jit.py poc/in/core.wasm`, guard (default), 5 pairs, self-check;
* compile time as in §2;
* the spec suite's wall time with and without `WABOROS_JIT_WALK_CHECK=1` (reported, not a bound).
aarch64 timings need the native runner: correctness only under QEMU (open question F).

## 10. Sub-steps

| sub-step | content | acceptance | budget (src / tests) | risk |
|---|---|---|---|---|
| 3a.1 | site records in both backends (+ aarch64 `rewind`), `gc.py` builder + encode/decode, compiler hooks, `Compiled.stackmaps`, `jit_dump.py` 4th column; masks in slot numbering (if A(a)) | `uv run pytest -q tests/jit tests/test_gc*.py`; `python3 tools/run_arm64.py -m pytest tests/jit/test_compiler_aarch64.py -q`; identity procedure (8 lists, columns 1-3 equal); builder/format tests | core ~120, x86 ~12, aarch64 ~14, tools ~5 / ~70 | low for bytes (no emission), R3 for the numbers (validated in 3a.3) |
| 3a.2 | word 14, store exit record, bridge and entry on x86-64 SysV + Win64 and aarch64, trampolines' table | `uv run pytest -q tests/jit` (x86); `python3 tools/run_arm64.py -m pytest tests/jit/test_abi_aarch64.py tests/jit/test_tail.py tests/jit/test_exceptions.py -q`; ISA tests over the trampolines; spec suite counts unchanged on `jit-x86_64` (guard, explicit) and `jit-aarch64` under QEMU (both); host-call and entry timings alternated; Win64 trampolines on the Windows cell of `platforms.yml` | x86 abi ~20, aarch64 abi ~16, runtime ~15 / ~10 | **R2 high**: hand-written trampolines (alignment L3, flags L1, nested entries L4, CET/BTI unchanged); Win64 runs only in CI |
| 3a.3 | `walk.py`, checked callbacks and refill, `ntop`, the refusal, `test_walk.py` | `WABOROS_JIT_WALK_CHECK=1 uv run python -m tests.spec.run --engine jit-x86_64` with `WABOROS_JIT_BOUNDS=guard` and `=explicit`; `WABOROS_JIT_WALK_CHECK=1 python3 tools/run_arm64.py -m tests.spec.run --engine jit-aarch64 --in-process` both modes; each equal to the run without the switch; `pytest tests/jit/test_walk.py` native and under QEMU; native arm64 CI job with the switch | runtime ~175 / ~100 | **R3**: the walk is new trusted code from step 5 on; a wrong table here is a wrong root later |
| 3a.4 | mutations (§8), timings and compile time (§9), docs | every mutant caught; CoreMark, host-call, entry within the spread; compile time <= +3 %; `python3 tools/check_docs.py` | docs only | — |

Total: src ~410 (core ~125, x86-64 ~32, aarch64 ~30, runtime ~190, tools ~5), tests ~180:
within the spec's ~420 / ~180. Stop and report if 3a.3 exceeds ~220 runtime lines.

**Docs** (3a.4, STANDARDS "Which document to update"): ARTIFACT.md part 2 (`STACKMAPS` row and
layout, the loader rule) and part 3 (word 14 row; per-architecture rows "exit record written by
the bridge (sp, pc, frame)" and "entry saves {exit record, frame} at sp+0 / sp+96"; the
`updated:` date); S0003 and S0004 Changes; DECISIONS.md: a new decision (precise roots by a
return-address walk and an exit record; context ABI v1 and the artifact format amended without
a bump, Q5; the refusal test) and a D39 amendment line (site records, both backends, code
byte-identical); TESTING.md (the check mode, how to run it); S0013 Progress and Changes (the
interface details beyond the spec text: slot numbering, pool framing, Trampolines fields, the
`throw` site); BENCHMARKS.md (host-call, entry, CoreMark, compile-time rows with conditions);
LESSONS.md for any trap met; ci.yml only if F is approved.

## Open questions (options and costs; nothing chosen silently)

* **A. Value positions against slots.** `refmasks` counts a v128 as one position; the core's
  slots count it as two. (a) The validator records masks in slot numbering (bit `i + number of
  v128 below`): ~5 lines in `validate._body`, the 1b tests' expectations unchanged except any
  with a v128 below a reference (none today), one sentence in S0013's 1b Changes; the step-1b
  contract changes (out of this step's listed scope: `frontend/`). (b) The validator records a
  second dict of v128 positions and the core converts: ~10 lines in two places, more data.
  (c) The core tracks types on its stack: touches the hot one-pass path. **Recommend (a).**
* **B. "A missing map bit" cannot be caught by the check mode at 3a**: externref handles do not
  move, so a dropped bit changes nothing at run time until step 5's stress mode. At 3a it is
  caught by exact-map unit tests only. Option: a check-mode extension that, at each walk,
  rewrites every marked externref slot to a fresh handle of the same object and retires the old
  one, so an unmarked live slot reads a retired handle; but handles in globals and tables are
  not walked at 3a, so retiring is unsound (~40 lines, false failures). **Recommend: unit tests
  now, the stress mutation at step 5.**
* **C. The per-function local bitmap** step 7's nulling needs is not in the `STACKMAPS` sketch.
  (a) Add it at step 7 as an amendment (no file is written before phase 5); (b) add a u32
  `locals map` index per site now (20 bytes per site, +25 % section size, unused until step 7).
  **Recommend (a).** Also fixed here (the sketch left them open): a `u32 nmaps` before the pool
  and 1-based pool indices.
* **D. Walker location:** a new `jit/common/walk.py` (one concern, reused by step 5's root
  gathering) or ~120 more lines in `instance.py` (990 lines). **Recommend `walk.py`** (CLAUDE.md
  asks for a named need for a new module: the spec names the walk as a runtime component and
  step 5 is its second user).
* **E. Instruction counts** differ from the spec's estimate: per host call 8 (x86) / 6 (aarch64)
  instead of "about five"; per entry 15 / 10 instead of "about eight" (save and restore). The
  cost bound (no change beyond the spread on host-call and entry loops) is what is measured.
* **F. CI.** The acceptance asks for native arm64 with the check on; today no CI job sets the
  switch. (a) Add `WABOROS_JIT_WALK_CHECK=1` permanently to one existing spec run per
  architecture (x86 `jit` explicit run, arm64 native guard run): cost = the measured slowdown of
  those runs; (b) a one-off CI run on the branch for the acceptance only. **Recommend (a)**,
  since the check exists to keep the layouts verified as code changes (it is also the safety
  net of steps 3b-7). aarch64 timings (host call, entry, CoreMark) also need the native runner.
* **G. Hand-assembled `Instance`s in `tests/jit/test_abi*.py`** have no `Compiled`; the
  refusal applies only to `JitInstance`. Under the check mode their host calls would report
  an unknown `pc`. (a) leave them out of check-mode runs (the spec suite is the acceptance);
  (b) pass `b.sites` from their backends into an optional `stackmaps` (their code is emitted by
  the same call methods, so it is walkable): ~10 lines of test helpers. **Recommend (b) only if
  the orchestrator wants `pytest tests/jit` itself under the check.**
* **H. Form of the D39 output:** a `sites` list on the backend (no signature change; the core
  pairs by count with an assertion) or a return value from each call method (signature change
  of six methods; `throw` would need to return its inner site). **Recommend the list.**
* **I. Refusal error type:** `ValueError` (bad input, as for cross-store imports) or
  `RuntimeError` (as "cannot run here"). **Recommend `ValueError`.**
* **J. ARTIFACT.md part 2 lists `ENTRIES` (entry trampolines) as artifact content**, while the
  runtime's trampolines are process-wide and the spec calls them "not module code". The
  entry's new duty is written in part 3 (per-architecture table); whether `ENTRIES` stays in
  part 2 is for the phase-5 format spec. No action at 3a besides a note.
