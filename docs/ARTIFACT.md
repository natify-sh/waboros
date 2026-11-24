---
title: JIT artifact, ISA levels and the context ABI
kind: reference
status: current
updated: 2026-11-24
---

# JIT artifact, ISA levels and the context ABI

A JIT target's output is plain machine code with a small, fixed boundary. Nothing in it is
Python-specific (measured: the same module compiled under CPython 3.8 and 3.13 gives byte-identical
code), so the artifact is **a language-neutral format**: any host that can map executable memory, call
a C function pointer and pass C callbacks can load and run it (C/C++, Dart FFI, LuaJIT, Ruby Fiddle,
Java FFM, .NET, Deno/Bun, MicroPython's `ffi`). This document is the contract such hosts — and our own
Python loader — rely on. It has three parts, each versioned.

> **Status:** working freeze of v1 (maintainer, 2026-11-23): the format of S0014 and the context ABI with S0015's boundary are the project's working ABI, not a release. Nothing in them changes silently: a change goes through a spec or a decision and updates the documents and the tests, without a version bump. The numbers in the file stay `format_version` 1 and `ctx_abi_version` 1 (0 in `EI_ABIVERSION` is what any ELF carries and is refused), so v1 is the current version. The final freeze of v1 comes before the merge to `main`, once the first sljit micro-compiler (D68) compiles code across the S0015 boundary (its C entries, the form-2a sibling tail calls and a record with no `tail_switch`, untested until then) and the C reference loader (D36) embeds it and reads and runs the committed files (ROADMAP.md, "Next" (6)). *(Amended 2026-11-24, maintainer: the final freeze of v1 comes once the first real micro-compiler, our core in C over sljit (D68), compiled to wasm, produces files across the S0015 boundary and the C reference loader (D36) reads and runs them and the committed files; S0016's `jit-sljit` is a step towards it, not part of the condition; the "none" `tail_switch` value leaves v1 (S0016), so the condition no longer names form 2a or a record without `tail_switch`.)* *(Amended again 2026-11-24, maintainer: the final freeze of v1 comes once the micro-compiler from our C sources (D68) is built to wasm and natively, the C reference loader with the native compiler embedded (x86-64, aarch64) compiles and runs modules, and the same loader built without the compiler runs files cross-compiled by the wasm build and the committed files.)*

> **Scope (DECISIONS.md D18).** First of all this is the format of the waboros runtime's own cache entries
> and serialized files (`Module.serialize`, `waboros compile`): waboros follows the Wasmtime model, the
> runtime lives in the package and nothing is embedded in the artifact. Loading from other languages
> stays possible because the boundary is neutral, but it is optional.

> The PoC (`poc/w2jit.py`, `w2jit2.py`) predates this contract: its context layout and entry signature
> are a subset of version 1 below and must be brought in line in phase 5.

## 1. ISA levels

An artifact declares the **instruction-set level** it was compiled for; a loader refuses an artifact
the CPU cannot run.

| architecture | baseline (default for portable artifacts) | optional levels / features the compiler may use |
|---|---|---|
| x86-64 | **x86-64-v1**: SSE, SSE2 (every x86-64 CPU) | `x86-64-v2` (SSE3, SSSE3, SSE4.1, SSE4.2, POPCNT), `x86-64-v3` (AVX, AVX2, BMI1/2, LZCNT, FMA, MOVBE), single features by name |
| aarch64 | **ARMv8.0-A** with FP and Advanced SIMD (NEON) | `lse` (v8.1 atomics), `fp16`, `dotprod`, `rcpc`, … by name |
| riscv64 | planned (D48, the third JIT architecture, after `aarch64.wasm`); defines its baseline here before its first release | RVV 1.0 as an optional level |
| later | none planned: every other architecture runs `pyc` (D48) | |

Rules:

* **Portable artifacts** (`waboros compile`, shipped files) use the baseline unless asked otherwise.
  An operation whose natural instruction is above the baseline gets a baseline sequence. Example found
  in the PoC: `f64.trunc` was emitted as `roundsd` (SSE4.1, x86-64-v2); at v1 it needs an SSE2
  sequence (convert-truncate for |x| < 2⁵², otherwise x itself, preserving sign and NaN). The same
  applies to `floor`, `ceil`, `nearest`, `popcnt`, `clz`, `ctz`.
* **Baseline first (D24).** Every operation is implemented at the baseline first; the baseline path is
  mandatory and is what every artifact uses by default. A path above the baseline exists only as an
  **optional, declared variant** next to its baseline twin, added when a measurement shows it pays;
  an above-baseline instruction the PoC already uses is kept this way, not dropped (known so far:
  `roundsd`, SSE4.1 / x86-64-v2, for `f64.trunc` in `poc/w2jit.py` and `poc/w2jit2.py`).
* **Local cache artifacts** may use `isa="native"` once optional variants exist: the features of the
  running CPU, recorded in the artifact and in the cache key, so a cache moved to another machine is
  recompiled, not executed.
* **Checked, not trusted**: CI disassembles generated code (`objdump`) and fails if any instruction is
  above the level the artifact declares.
* `Config.isa` (waboros addition): `"baseline"` (default), a level name, or a level plus feature names
  (explicit opt-in, stage 1 of D33); `"native"` comes later (stage 2) and becomes the default only by a
  separate decision. A requested level the CPU lacks is refused at load, naming the missing features.
* **Detection at load time, stdlib only**: on x86-64 a small `cpuid` stub run through the same
  executable-memory + ctypes path (works on every OS); on aarch64 `getauxval(AT_HWCAP/AT_HWCAP2)` via
  ctypes on Linux/Android, `sysctlbyname("hw.optional.*")` on macOS, `IsProcessorFeaturePresent` on
  Windows. Hosts in other languages do the equivalent.

A host also supplies `entry` as machine code of its own (S0015, D70): the file holds none. Part 3's
"Entry" is its contract, and "Reference listing of the runtime's `entry`" there gives a listing per
architecture and convention. Planned: hex bytes of each listing, for hosts without an assembler
(Dart FFI, LuaJIT).

## 2. Artifact format (version 1)

The byte-exact definition is [S0014](specs/S0014-artifact-format.md); this part is its summary and
the contract's version line. A file is an **ELF64** relocatable object (D59; the analysis is in
[ARTIFACT-CONTAINER.md](ARTIFACT-CONTAINER.md)): `ET_REL`, little-endian only, private `EI_OSABI`
0xCB, `EI_ABIVERSION` = `format_version`, `e_machine` 62 (x86-64), 183 (aarch64) or 243 (riscv64,
reserved), no program headers. No OS loads it; the waboros loader maps `.text` itself. Self-contained:
a host needs **no wasm parser** to instantiate it. Written and read by
`targets/jit/common/artifact.py`; deterministic (D35); named `<name>.<target>-<level>[-guard].wbo`.

**Sections**, in a fixed order, each payload parsed exactly; `.waboros.*` sections are found by
name and unknown sections are skipped (D62):

| section | content |
|---|---|
| `.waboros.header` | `module_sha256` (first 32 bytes, frozen across versions), `config_sha256`, `ctx_abi_version` (1), `isa_level`, `flags` (bit 0 `BOUNDS_GUARD`, bits 8-15 `GUARD_LOG2`, S0006), `isa_features`, `features_required` and `features_optional` (D66, empty in v1), `compiler` (`waboros <version> <target> <target version>`) |
| `.text` | the machine code, position-independent, at a file offset of 4096; then its data area (jump tables); then the compiler's trampoline blobs |
| `.waboros.types` ... `.waboros.segments` | `types` (with the 32-byte canonical type keys), `imports`, `funcs` (type, code offset, `data_start` and the start function), `tables`, `memories`, `globals`, `tags`, `exports`, `segments`; constant expressions in a fixed-width form (`cexpr`) |
| `.waboros.context` | what the context layout (part 3) needs beyond the counts: the heap and subtype bits, the function records, signatures, thunks, RTTs and thrown tags |
| `.waboros.stackmaps` | the frame walk's site table (below). Required |
| `.waboros.traps` | the guard fault map `{fault_offset, resume_offset}` of S0006 (empty without `BOUNDS_GUARD`); the address map for `Trap.frames` is not produced (a later optional `.waboros.addrmap`) |
| `.waboros.trampolines` | one 28-byte record per host convention (`sysv`, `win64` on x86-64; `aapcs64` on aarch64: one convention on Linux, macOS and Windows) for the compiler's bridge and `tail_switch` in `.text` (S0015: `entry` and `sp_stub` are the host's, not in the file); a store runs on one copy (S0014, "One copy per store") |
| `.waboros.wasm` | optional: the original wasm bytes. Its presence makes a **fat** file (below) |
| `.symtab`, `.strtab`, `.shstrtab` | for tools (`objdump`, `readelf`, `gdb`); every symbol local; no loader reads them (D62) |

What the first, container-less draft of this part had and v1 does not: `START` is a field of
`.waboros.funcs`; `RELOCS` is absent (the code is position-independent; a `SHT_REL`/`SHT_RELA`
section is refused); `CHECKSUM` is dropped (S0014 Q5: the strict reader refuses truncated and
malformed files, and cache writes are atomic); `FUNCS`, `TRAPMAP` and `STACKMAPS` became
`.waboros.funcs`, `.waboros.traps` and `.waboros.stackmaps`; `ENTRIES` became
`.waboros.trampolines` and the blobs in `.text`.

**`.waboros.stackmaps`** (part 2's `STACKMAPS`; S0013 step 3a; added to version 1 without a bump, as no artifact file has been
written, S0013 Q5). Little-endian: `u32 nsites`; per site, sorted by `ret`: `u32 ret` (the code
offset of the return address of a call), `u32 shift` (slots from this frame's base to the callee's:
0 for a host or service call, in a host thunk and for a C entry's call into its body), `u16 ret_at` (bytes from the stack pointer at the
call, before x86-64's `call` pushes, to this frame's own return address), `u16 up` (bytes from that
stack pointer to the caller's stack pointer at its own call), `u32 map` (0: no reference slot; k:
the k-th map of the pool); then `u32 nmaps` and per map `u32 nslots`, `ceil(nslots / 32)` u32
words of `refs`, bit i set when value-stack slot i of the frame (locals first, a v128 two slots)
holds a reference of the any, extern or exn hierarchy (exn from S0013 step 6.2) at that site, and
as many words of `live`, a subset of `refs` (S0013 step 7, D65). A walk forwards the `live` slots
and writes null into the slots of `refs` that `live` does not mark; `live` holds the reference
locals live at the site, from the shared core's liveness pass (S0013 step 7.2), and the operand
bits of `refs` (`WABOROS_JIT_NOLIVE` gives `live = refs`, the step-7.1 tables). A map is never empty and equal maps (`nslots`, `refs`, `live`) are
stored once. Every call site has a record, a module without any reference type too (16 bytes per
site: CoreMark 115 sites, 1 848 bytes on both JITs since S0015, no map). The two-bitmap form (S0013 step 7.1) also
amended version 1 without a bump; instantiation decodes the table with checks and refuses
(`ValueError`) one that does not parse exactly, has a map index past the pool or a `live` bit
outside `refs` or whose sites are not strictly increasing by `ret`, so a table in step 3a's one-bitmap form with a map is refused in the shapes tested, not by
contract: the table has no version field, and a 3a table can be built that parses (the 7.2 review);
versioning is the artifact format's: one `format_version` for the whole file, no per-section
version, amendments inside a version through feature flags (D66). One without
a map, the same bytes in both forms, still runs. The trampolines' own points (the entry's return point and its saved block, `tail_switch`'s
`restore` frame) are part 3's, not this section's.

Strings are UTF-8 with a u32 length prefix. The reader's further checks (S0014 has the full text):
a supertype chain is at most 63 deep; limits obey the validator's (`min` <= `max`, memories at most
65536 pages or 2^48 for 64-bit addresses, tables at most 2^32-1 or 2^64-1); the signature words are
function types; every `resume_offset` of the fault map is below `data_start`; stack-map sites are
strictly increasing by `ret`; no constant expression allocates when `heap` is 0; no token of the
`compiler` string is empty. Metadata is not cross-checked against the code (S0014, "Known
limitations of v1"). A loader refuses an artifact whose ELF identification,
`format_version` (`EI_ABIVERSION`), `ctx_abi_version`, `e_machine` or ISA it does not support, or whose `compiler`
string is not the store's (D70: until phase 6 the exact string, which is the cache key; from
phase 6 the rule follows the architecture, `format_version` and `ctx_abi_version`, S0015
"Compatibility") — it never "tries". A store may hold modules of several compilers of one
architecture in one process (S0016): each file is loaded under its own target's exact string (the
caller passes the target; the loader does not dispatch on the string), and instantiation refuses
two targets that share one bridge address (D71). The trampoline record never holds `0xFFFFFFFF`
(no "none" value in v1, S0016); `read` refuses it.
`Module.serialize()` writes this format; the cache stores it.

**Thin and fat files (D18).** A thin file (no `.waboros.wasm`) is refused on any mismatch. A fat file is
never refused: when the loader cannot use the code — another waboros or target version, another
compiler string than the store's (D70; phase 6 relaxes it to the architecture's rule), another ISA level, a compiler version on the known-bad list, a format it does not know — it checks that the
embedded wasm matches `module_sha256` and regenerates from it. For this the loader must be able to
read the header and find `.waboros.wasm` in every format version, so the ELF identification bytes
0-8, the section header table's own fields, the names `.waboros.header` and `.waboros.wasm`, the
first 32 bytes of the header (`module_sha256`) and the content of `.waboros.wasm` are frozen across
versions (S0014, "Frozen across format versions").

**Other targets.** Not in format v1. Serialized `pyc` and `mpy` modules will use the same ELF
container with `e_machine` `EM_NONE`, the compiler target `pyc-cp313` (or `mpy-6`) and the marshal
data or `.mpy` in a `.waboros.pyc` section (S0014 Q6); JIT loaders refuse `EM_NONE`. The extension
is `.wbo` (S0014 Q7).

## 3. Context ABI (version 1)

What compiled code expects from its host at run time. Any change to a word's meaning or position is a
new `ctx_abi_version`; new words are only ever appended in reserved space, and such an amendment
is announced by a `features_required` bit (D66), so an older loader refuses the file.

**Entry.** The host's `entry` (the runtime's own; `.waboros.trampolines` holds only the
compiler's bridge and `tail_switch`, S0015 step 4) is a C function in the host's calling convention:

```c
int32_t entry(uint64_t *ctx, uint64_t *frame, const void *code_of_function);
```

*From S0015 steps 1 (x86-64) and 3 (aarch64)* ([S0015](specs/S0015-boundary-convention.md)):
`code_of_function` is a **C entry**, `int32_t f(uint64_t *ctx, uint64_t *frame)` in System V on
every OS on x86-64 (Windows included: only the runtime's `entry` and conforming code call it), in
AAPCS64 on aarch64. `entry` is the
runtime's, a contract with the reference listing below: it saves the host's
callee-saved registers the boundary does not keep (Win64: `rsi`, `rdi`, `xmm6`–`xmm15`), keeps
the saved block (below) at `[rsp, rsp + 32)` of its call, calls the C entry, puts the exit record
back, takes the status as 32 bits and keeps a non-zero one in `TRAP_CODE`. It sets up no
register: each compiler's C entry does. `jit-x86_64`'s C entry is a 16-byte stub before each body
and host thunk (`endbr64; lea rdx, [rip + 5]; jmp centry`) and one `centry` per module after the
last thunk, which saves `r12`–`r15`, sets up the compiler's registers and calls the body (a site
with shift 0 and no map). The file's trampoline blob (S0015 step 4) holds the compiler's bridge and
`tail_switch` only: the `entry` and `sp_stub` are the host's. On aarch64 the `entry`
saves only `x19` (with `x20`), keeps the saved block at `[sp + 96, sp + 128)` of its `blr` and
zero-extends `w0` before it keeps the status; it and `sp_stub` start with `bti jc`.
`jit-aarch64`'s C entry is a 12-byte stub (`bti jc; adr x16, body; b centry`), the body at
`code + 12`, and one `centry` per module after the last thunk, which saves `x25`–`x28` under a
frame record (`x19`–`x24`, `x29` and `d8`–`d15` its bodies keep), sets up the compiler's registers
and calls the body (a site with shift 0 and no map: `ret_at` 8, `up` 48).

* `frame` points into the host-allocated **value stack**; the arguments are written to `frame[0..n)`,
  the results come back in `frame[0..m)`, one 64-bit slot per value (i32 zero-extended in the low half;
  i64 as is; f32/f64 as bit patterns; v128 takes two slots, the low 64 bits (bytes 0–7) in the first
  slot or word, as for a v128 global; references as pointer-sized values).
* The return value is `0` or a trap code (below). On a trap the machine stack is unwound to the
  trampoline, which returns normally: the host never sees a signal or a non-local jump.
* **Re-entrant**: the trampoline keeps nothing in the context but `TRAP_CODE`, so wasm → host → wasm
  nesting works (the PoC lacked this; LESSONS.md 4).

**Context words** (`uint64_t ctx[]`, 8-byte aligned, at a stable address for the instance's lifetime;
the phase-3 meaning of each word and the register roles are in [S0003](specs/S0003-jit-context-abi.md)):

| index | name | direction | meaning |
|---|---|---|---|
| 0 | `SAVED_SP` | internal | zero (traps return through frames, and the guard-page fault handler resumes at a stub, S0006, so neither uses it); kept reserved |
| 1 | `TRAP_CODE` | code → host | written by the trampoline when it returns a non-zero status |
| 2 | `INTERRUPT` | host → code | non-zero: the code returns `INTERRUPT` at its next poll (any thread may set it); a host sets a value with non-zero low 32 bits, since code may test only the low 32 (32-bit rule below) |
| 3 | `FUEL` | both | remaining fuel (signed), when compiled with fuel |
| 4 | `EPOCH_DEADLINE` | host → code | when compiled with epochs |
| 5 | `VSTACK_LIMIT` | host → code | address the value stack must not cross → `STACK_OVERFLOW` |
| 6 | `NSTACK_LIMIT` | host → code | lowest native stack address allowed → `STACK_OVERFLOW` |
| 7 | `HOST_ERROR` | host → code | the status of the current host call: the bridge presets 13, the host function writes its status last (D34) |
| 8 | `HOST_BRIDGE` | host → code | address of the host-call bridge for the host's calling convention; one copy per compiler, so `jit-x86_64` also compares it to recognise a context of its own code (D71, from S0015 step 1); a copy is never shared between compiler strings, even with identical bytes: equal addresses mean the same compiler string and internal convention |
| 9 | `TAIL_SWITCH` | host → code | address of the cross-context tail-call trampoline (below; D30) of the context's own compiler (S0015, D70); never 0: every compiler ships a `tail_switch` (the "none" value left v1, S0016; form 2a only beside one) |
| 10 | `EXCEPTION` | host → code | address of the store's pending-exception word: the exnref of the exception in flight, valid only while status 14 propagates (S0012); from S0013 step 6.2 it holds 0 or the zero-extended u32 heap offset of the last exception thrown, and the word is a root of every collection |
| 11-13 | reserved | | zero (S0013 step 6.3; the exception arena's refill service, cursor and limit until then, S0012; an amendment of v1 without a bump, Q5) |
| 14 | `EXIT_RECORD` | host → code | address of the store's exit record `{sp, pc, frame}` of the innermost host call: the bridge writes it before the host runs (the calling frame's stack pointer at its call, the return address, the frame base of the calling wasm frame, the base its site's map refers to) and clears `sp` after; every entry keeps the enclosing record and its frame on the native stack during its call and puts the record back (S0013; an amendment of v1 without a bump, Q5) |
| 15 | reserved | | zero |
| 16 … | per memory *i*: `base`, `size_bytes` (2 words) | host ↔ code | imported memories first; a non-shared memory's base and `size_bytes` may both change during any host call, libcall or record call, for an i32 memory as for an i64 one (a `memory.grow` that moves it; D50, S0009: an amendment of v1 without a bump; maintainer, 2026-11-24), so code keeps neither across a call; the 4 GiB + 32 MiB reservation of an i32 memory (committed on grow; S0006) is a property of the 64-bit runtime, not of the contract, and a shared memory never moves (a 32-bit runtime commits it at its maximum): code that keeps a memory in registers (x86-64: memory 0 in r13/r14; aarch64: x26/x25) reloads them after every host call (and, from S0015 steps 1 and 3, after every call through a function record: a callee may grow an imported memory), any other memory is read here at each access |
| … | the GC heap: `base`, `size_bytes` (2 words), right after the module's memories | host ↔ code | only in a module that uses GC (GC types or instructions, or a `call_indirect` / `return_call_indirect` to a non-final type; S0013 step 3b, an amendment of v1 without a bump, Q5) or, from S0013 step 6.1, allocates or reads exception objects (a `throw`, or a `try_table` with a `catch` or `catch_ref` clause; a module with only `throw_ref` and `catch_all(_ref)` has none): the store's heap, an i32 memory (4 GiB + 32 MiB reserved in the 64-bit runtime; its base may change at any host call, libcall or record call, so code reloads it, maintainer, 2026-11-24) that `memory.size`, `memory.grow` and the libcall immediates do not count; a module without memories of its own has it as memory 0 (r13/r14, x26/x25). `size_bytes` is the highest committed end; the heap's regions are committed apart, so gaps below it are uncommitted and no code addresses them (D69, an amendment of v1 without a bump) |
| … | per table *i*: `base`, `size` (2 words) | host ↔ code | `base`: the element words (one per element), `size`: elements; `table.grow` may move the elements, so code reads both words at each use and never keeps them across a host call. A `funcref` element (and value) is 0 (null) or the address of a function record (below); an `externref` element is 0 (null) or a reference word: a host value is `(h << 2) \| 2`, *h* its 1-based handle in the store (S0013 step 3b; the encoding is the runtime's, no code reads the bits); an `exnref` element is 0 or an exception object's heap offset (S0013 step 6.2) |
| … | per global *i*: 1 word (2 for v128) | both | imported globals: a pointer to the owner's word instead |
| … | per function import *i*: 1 word | host → code | address of a host function (signature below) |
| … | the libcall block: 10 words, per libcall *j* 1 word | host → code | a runtime service, same signature as an import, in a fixed order, all ten words in every context: 0 `memory.grow`, 1 `table.grow`, 2 `table.init`, 3 `elem.drop`, 4 `memory.init`, 5 `data.drop`, 6-9 reserved, zero (D58: threads' `memory.atomic.wait32`, `wait64` and `notify` go in words 6-8 as an amendment of v1; word 9 is earmarked for an atomic read-modify-write service, an op immediate after the operands, for architectures whose atomics mapping routes atomics to the runtime (no 64-bit or no atomic instructions: ARMv6-M, ARMv7-M 64-bit, RV32IMC, mips32, ppc32; maintainer, 2026-11-24); S0014 step 0, an amendment of v1 without a bump: no artifact was written before it, and every word after the block moved by 4); never computation (D25). The operands are in the slots, the instruction's immediates (table, segment) after them; the memory index of `memory.grow` and `memory.init` only when the module has more than one memory. Operands and results have the address width of the memory or table they name (S0009, an amendment of v1 without a bump, Q4): for an i64 memory the delta, the old size and its failure −1 of `memory.grow` and the destination of `memory.init` are 64-bit values (−1 is `2⁶⁴−1`), for an i32 one they are i32 zero-extended as before; `table.grow` and `table.init` of an i64 table the same way (S0009 step 3) |
| … | per signature *k*: 1 word | host → code | the canonical type id of a type the module's `call_indirect`s name (equal for equivalent types of every module of the process, never 0) |
| … | per function record *r*: 3 words | host → code | `{code address, canonical type id, context address}` of every function import and of every function the module can take a reference to (`ref.func`, segments): a defined one; an import from another instance of the same store (its code and context: a direct call); a host function (the module's thunk, which calls the import's word through the bridge). `call_indirect` checks bounds, null and the type id, then calls `code` with r15 = `context` (and that context's memory words); `call` and `return_call` of an import go through the import's record the same way (D32). *From S0015 steps 1 (x86-64) and 3 (aarch64):* `code` is the callee's **C entry** (a defined function's, an exporter's for an import from another instance, the importing module's host thunk's for a host function); a call through a record saves `rsi rdi r8`–`r11` (the pool registers a C entry may clobber, for every frame above), passes `rdi` = `context` and `rsi` = its frame base + 8 · shift, and restores them and reloads memory 0's words before it tests the status, on every path. A compiler may bypass the C entry for callees it recognises as its own, a private choice (D71): `jit-x86_64` calls a record of the caller's own context at its body (`code + 16`) as a direct call, and one of another context whose word 8 (`HOST_BRIDGE`) equals the caller's (the same compiler) at its body with `r15` saved and switched and memory 0's words the callee's. `jit-aarch64` does the same: it saves `x2`–`x8` around a C entry (`x0` = `context`, `x1` = the frame), calls a body of its own context at `code + 12`, and one of the same compiler with `x28` saved in 16 bytes and switched |
| … | per tag *t*: 1 word | host → code | the tag's identity: non-zero, unique in the process, imported tags first; an imported tag has its exporter's value, so a `catch` compares identities, never indices (S0012) |
| … | the GC group: 8 service words | host → code | a module with the GC heap only (above; S0013 step 3b), runtime services with the libcall signature, in this order (operands, then immediates, in slots): 0 `ALLOC` (`[bytes]`: room past the cursor; the code bumps it when it retries), 1 `ARRAY_NEW` (`[value low, high, n, type]` → object), 2 `ARRAY_NEW_DATA` and 3 `ARRAY_NEW_ELEM` (`[offset, n, type, segment]` → object), 4 `ARRAY_FILL` (`[array, i, value low, high, n, type]`), 5 `ARRAY_INIT_DATA` and 6 `ARRAY_INIT_ELEM` (`[array, d, s, n, type, segment]`), 7 reserved, zero (`FUNC_SUBTYPE` until S0013 step 5.6; no code uses it). An element value is always two words (a scalar's second is 0) |
| … | per RTT *k*: 1 word | host → code | a module with the GC heap only: the offset in the heap of the type-info record of each struct or array type the code allocates inline or casts to (equal for equivalent types of every module of the store), then (S0013 step 6.1) of the exception record of each tag the code throws (by tag index; tags of equal type share the record) |
| … | `IS_SUBTYPE`: 3 words | host → code | a module with the GC heap only (S0013 step 5.6, an amendment of v1 without a bump, Q5: appended, no word moves): a function record `{code, 0, context}` of the collector module's `is_subtype(q, t: i64) -> i32` in the store's collector instance (from S0015 steps 1 and 3: `code` is its C entry); written when the module's code has a function-subtype site (below), else zero |

Offsets of the variable part follow from the counts in the artifact, in exactly this order.

**32-bit architectures** (provisions for a future one, which may come only in ABI v2; v1 must not
block it; "+"maintainer, 2026-11-24"+"): words stay 64 bits wide and little-endian. On a 32-bit architecture the high
half of every pointer or size word (bases, sizes, limits, addresses, records) is zero and code may
read only the low half; canonical type ids and tag identities are below 2³² (the runtime's tag
counter, `range(1, 1 << 63)` in `targets/jit/common/instance.py`, will need a cap: ROADMAP); a
host sets `INTERRUPT` to a value with non-zero low 32 bits, and code may test the low 32 bits.
Big-endian 32-bit ISAs (MIPS BE, PPC32) are accepted as v2 (ROADMAP, phase 8).

**The GC heap** (S0013 step 3b; the collector and its regions B come with step 5). The code's contract
is heap offsets 0-15 (null, the cursor at 8, the limit at 12, the flag), the object, type-info record
and reference-word formats and the RTT words; the region layout below (type area, tid map at
60 MiB, `ROOTS`, the handle report, the semispaces, the 64 MiB start) is private to the runtime and
its collector module, so a small-memory runtime may differ ([GC-SMALL-MEMORY.md](GC-SMALL-MEMORY.md);
maintainer, 2026-11-24). Offsets in the
heap: 0-7 zero (null is 0), the allocation cursor (u32) at 8 and its limit (u32) at 12; the rest
of the header page belongs to the collector module (S0013 step 5.2, `runtime/gc/collector.wat`):
at 16 the handle-report overflow flag (u32), 20-2047 reserved, [2 KiB, 4 KiB) the handle report (bit
*h* set for each host word `(h << 2) | 2` a collection found reachable, *h* < 16 384; a larger *h*
sets the flag instead) and [4 KiB, 64 KiB) `ROOTS`, the buffer of 7 680 u64 root words the module
forwards in place (the module traps on a malformed heap, but without a map of object starts it
cannot tell an aligned interior offset from an object start: S0013 Changes, 5.2); type-info
records in [64 KiB, 60 MiB), committed after the header page in 64 KiB pages as they are written
(the rest uncommitted, D69); the **tid map** in [60 MiB, 64 MiB) (S0013 step 5.6): the u32 offset
of the record of process-wide function type id *t* (ids count every function type the JIT sees in
the process) at `60 MiB + 4t` (0: none), for *t* < 2²⁰,
committed whole with the first function-type record (a type whose id would pass the cap fails the
instantiation with "GC type area exhausted"); objects from 64 MiB in two adjacent semispaces `[64 MiB, 64 MiB +
C/2)` and `[64 MiB + C/2, 64 MiB + C)`, both committed (`size_bytes`, the highest committed end,
is `64 MiB + C` once there are objects; before, the tid map's end or the type area's), whose
boundary moves when the heap grows, with `64 MiB + C <= 2³² − 128 KiB` (D61). Code allocates by
`cursor + size` against the limit (in 64 bits) and calls `ALLOC` when it does not fit; the cursor
and the limit lie in the active space, and a collection (in `ALLOC` or an array service) moves
them into the other half. A
**reference word** of the any or extern hierarchy is 0 (null), `(v << 1) | 1` for an i31, `(h << 2)
| 2` for a host value (*h* its 1-based handle in the store; at most 2³⁰ − 1, so a word fits 4
bytes), or an object's offset (a multiple of 8). An `externref` element or value is such a word;
an `exnref` is 0 or an exception object's offset (S0013 step 6.2), and slots, globals, table
words and the pending word hold every such word zero-extended to 64 bits.
**Objects**: a u32 type-info offset and a u32 of collector bits (0 while wasm runs); a struct's
fields from 8 in declaration order at their natural alignment (`i8` 1, `i16` 2, `i32`/`f32` 4,
`i64`/`f64` 8, `v128` 16 (unaligned access), an any-, extern- or exn-hierarchy reference 4 (exn
from S0013 step 6.2, 8 before), `funcref` 8), its size a multiple of 8 and at least 8; an array's u32 length at 8 and its elements
from 16. **Type-info records** (u32 words): `kind | final << 8` (1 struct, 2 array, 3 function),
the depth *d*, the struct's size or the array's element size (0 for a function type), the number
of traced reference fields (an array: 1 if its elements are references of the any, extern or exn
hierarchy; 0 for a function type), the display (*d* + 1 record offsets, the last its own), then
the traced fields' offsets. A cast to a final type compares the object's record with the RTT word;
to a non-final one, the display entry at the target's depth. A **function-type record** is written
at instantiation for every function type of a module with the heap that has a supertype or is not
final, its supertypes first, and entered in the tid map; no object has one. An **exception
record** (S0013 step 6.1) is a kind-1 record, final, depth 0, written at instantiation for the
type of each tag a module throws: the object is a struct of the u64 tag identity at 8, then the
tag's parameters at their natural alignment as struct fields; its key is the type's canonical key
prefixed `exn `, so it is a different record from that type's function-type record. Its traced
fields are the parameters of the any, extern and exn hierarchies. **A function-subtype
site** (`call_indirect`, `return_call_indirect`, `ref.test`, `ref.cast`, `br_on_cast(_fail)` to a
concrete function type): the function record's type id (word 1) against the signature word; equal
is a match; else a final target fails, and a non-final one calls `IS_SUBTYPE` with `q` (the
record's id) and `t` (the signature word) as the callee's frame above every slot the site reads
after it (B1): 1 is a match. `is_subtype` answers 0 for an id at or past the cap or without a map
entry (equality was tested already) and traps on a malformed record.

**Host functions** (imports and libcalls) share one C signature in the host's calling convention:
`int32_t f(uint64_t *ctx, uint64_t *slots)` — the arguments in `slots[0..n)`, one 64-bit slot per
value as for entries; the function writes its results to `slots[0..m)`, then, as its last action, its
status to `ctx[HOST_ERROR]`: `0`, a trap code, or `13` `HOST_ERROR` (the host keeps the actual
exception). Its C return value is ignored (D34). Compiled code calls it through the bridge
(`ctx[HOST_BRIDGE]`), which saves what its compiler needs kept (private to the compiler since S0015), presets `ctx[HOST_ERROR]` to 13, returns the word as the call's status and puts back the word's
previous value (an enclosing host call's preset): a host function that does not finish fails the call.
A host function writes only `0`, a public trap code or `13` into `HOST_ERROR`, so no private status
leaves through a bridge (S0015).
A host function may enter wasm again; the nested entry's frame starts at `slots + max(n, m)`, right
above the call's slots, so no value-stack slot above them survives the call. Compiled code keeps
nothing it reads after a host call there (S0013 B1; the core asserts it at every call site; an
amendment of version 1 without a bump, as no artifact file has been written, S0013 Q5).

**Exceptions** (S0012, D52; an amendment of version 1 without a bump, as D34). An exception travels
as status `14` returned through frames, as a trap does; inside a `try_table` a call site's non-zero
status goes to the try_table's landing pad, which dispatches only 14 and passes every other status
on. From S0013 step 6.2 (D63) an **exception object** is an object of the GC heap with its tag
type's exception record (above): the u64 tag identity at 8, the payload as struct fields from 16
at their natural alignment; an `exnref` is its offset, null 0. `throw` allocates it as `struct.new` does (the cursor against
the limit, a cold `ALLOC` call when it does not fit, which may collect; the payload is read from
its slots after the call), writes the payload and the identity, stores the offset in the pending
word (word 10's target) and returns 14. A failed allocation ("GC heap out of memory") is status
13, which no pad catches. A landing pad reads the identity and the payload through the heap
memory's words with checked loads and compares the identity in 64 bits. Until step 6.2 objects
were 8-byte words `{tag identity, payload words…}` in a store arena that `throw` bumped through
words 12 and 13, calling the refill service of word 11 for a new chunk (S0012); step 6.3 removed
the arena. At an entry, status 14 is the
host's `Trap(UNCAUGHT_EXCEPTION)` with the tag and the values, read from the object before any
wasm or collection runs; a host function never returns 14 (a re-raised `UNCAUGHT_EXCEPTION` trap is status 13).

**Tail calls** (D30). A `return_call*` puts its arguments at its own frame base, drops its native
frame and jumps, so both stacks stay constant. *The rest of this paragraph describes both
architectures before S0015 (a history): x86-64 follows the next paragraph from S0015 step 1,
aarch64 from step 3.* When the callee's function
record names another context, the code jumps to `ctx[TAIL_SWITCH]` instead, with the record's address and the caller's
return address where the architecture's table below puts them.
The trampoline (one copy per store and convention, S0014 "One copy per store", in the host's code
area like the bridge) does the following. If that return address is its own `restore` point, it loads the record's context and jumps
to its code. Otherwise it saves the current context, loads the record's context and memory words, and
calls the code; at `restore` it puts the saved context back, reloads the memory words and returns the
callee's status. Every return matches its call. The restore frame is 16 bytes on both
architectures, the saved context at the stack pointer of the `call`/`blr` and the real return
address 8 above it; the GC's frame walk steps over it by these numbers (S0013).

*From S0015 steps 1 (x86-64) and 3 (aarch64)* (S0015, "Tail calls across the boundary", form
2b): a chain of `return_call*` across contexts runs in constant native stack and every return
matches its call. Our own context: a jump to the record's body (`code + 16`, behind its C entry).
Another context: when the caller's return address is its own module's C-entry return point (it
was entered through the boundary), the body returns the **private** hop status `0x80000001` with
the record in `rdx`; the C entry restores `r12`–`r15` and jumps to the record's C entry with
`rdi` = its context and `rsi` = the frame (a C sibling call). Otherwise the body jumps to
`ctx[TAIL_SWITCH]`. On the first hop it saves `rsi rdi r8`–`r11` and `r15` and calls the record:
the body behind its C entry with `r15` and memory 0's words switched when the record's context
has the caller's `HOST_BRIDGE` (the same compiler, D71), else its C entry; at `restore` it puts
them back and reloads memory 0's words. A hop whose return address is already `restore` only
switches and jumps the same way. That restore frame is 64 bytes: the real return address 56
above the stack pointer of its `call`, the caller's stack pointer 64 above it (the walk's
numbers, in `.waboros.trampolines` since S0015 step 4: `restore_ret_at` and `restore_up`). On aarch64 (step 3) the same with the
body at `code + 12`, the record in `x16`, the hop status returned with `ret` to the C entry (found
by `adrp`), which restores `x25`–`x28` and its frame record and branches to the record's C entry
with `x0` = its context and `x1` = the frame and `x30` our caller's return address; the first hop
saves a frame record `{x29, x30}`, `x2`–`x8` and `x28` and calls with `bl`, its restore frame 80
bytes: the real return address 8 above the stack pointer of that `bl`, the caller's stack pointer
80 above it.

**Per architecture** (S0004, D38; an amendment of version 1, which phase 5 fixes). A host never
provides a compiler's bridge or `tail_switch`; they come from the artifact. The first table is
fixed per architecture, the same for every compiler (S0015, D70), and is what a host must follow.
Everything else in this part is the same 64-bit little-endian words for every architecture and
compiler.

**Code addresses** (function records, `HOST_BRIDGE`, `TAIL_SWITCH`, import words) are in the ISA's
callable form (bit 0 set on Thumb, say). The walk clears the ISA's mode bits of a return address
before matching it against a `ret` (Thumb bit 0; windowed Xtensa bits 31:30, where the host must
also spill the register windows before a walk). S0015's headroom numbers (1 KiB, 768 bytes,
`nlimit − 4096`) are per-architecture values of the boundary row; x86-64 and aarch64 keep today's
(maintainer, 2026-11-24).

| | x86-64 | aarch64 |
|---|---|---|
| boundary (C entry), from S0015 steps 1 and 3 | `int32_t f(rdi = ctx, rsi = frame)` in SysV on every OS: keeps `rbx rbp r12`–`r15`, may clobber `rax rcx rdx rsi rdi r8`–`r11`, every xmm register and the flags; status in `eax` (the upper half undefined); DF 0 and MXCSR `0x1F80` control bits at entry and exit; `endbr64` first | `int32_t f(x0 = ctx, x1 = frame)` in AAPCS64 on every OS: keeps `x19`–`x29`, `sp`, `d8`–`d15`, may clobber `x0`–`x17`, `x30`, the other vector state and NZCV; never touches `x18`; status in `w0` (the upper half undefined); FPCR at entry and exit as S0015's table; `bti jc` first |
| exit record (bridge): `sp`, `pc` | `sp` = rsp + 8 at entry (before the `call` pushed), `pc` = `[rsp]` | `sp` = sp at the `blr`, `pc` = `x30` |
| entry's saved block | `[rsp, rsp + 32)` at its `call`: enclosing `{sp, pc, frame}`, then its frame | `[sp + 96, sp + 128)` at its `blr`, the same four words |
| landing pad, boundary | `endbr64` | `bti jc` (C entries, `entry`, `sp_stub`; S0015 Q5) |
| `.text` alignment | none | load address ≡ 0 mod 4096 (`adrp`) |
| instruction cache | coherent | invalidate the loaded range before the first call |

Compiler-private rows, keyed by the target in the header's `compiler` field, given for `jit-x86_64`,
`jit-aarch64` and the development target `jit-sljit-x86_64` (S0016, sljit's SysV register map on
every OS) as information (another compiler, a sljit micro-compiler, states its own):

| | x86-64 | aarch64 | `jit-sljit-x86_64` |
|---|---|---|---|
| bridge call | `eax` = word, `rdx` = slots; `call` | `x0` = word, `x1` = slots; `blr x16` | `rax` = word, `rsi` = slots; `call` |
| bridge preserves | pool (`rbx rbp rsi rdi r8–r11`), `r12`–`r15` | `x2`–`x8`, `x19`–`x28`, `x29`, `sp` | `rbx rbp r12`–`r15`, and `rcx r8 r10 r11` saved in its frame (`win64`: above the 32-byte shadow space, the host call custom bytes `mov rcx, rbx; mov rdx, rsi; call r13`) |
| `tail_switch` | record in `rdx`, return address at `[rsp]`; the first hop and the hops from `restore`, never one from a C entry; restore frame 64 bytes (S0015 step 1, D71) | record in `x16`, return address in `x30`; the same hops; restore frame 80 bytes (S0015 step 3) | record in `rax`, return address at `[rsp]`; the same hops; restore frame 48 bytes (`restore_ret_at` 40) |
| exit record (bridge): `frame` | `r12` | `x27` | `r15` |
| landing pad, the compiler's own | `endbr64` | `bti c` (bodies, bridge, `tail_switch`), `bti j` (jump-table pads) | `endbr64` |

**Reference listing of the runtime's `entry`** (S0015, Q3 (a); moved here from the spec's appendix).
A listing for the C loader and for review, not the waboros runtime's bytes. `EXIT` is
`8 · EXIT_RECORD` (112) and `TRAP` is `8 · TRAP_CODE` (8). Labels and register choices are free;
the saved block's place and steps 1–5 of the contract (S0015, "The runtime's `entry`") are not. The x86-64 listings are GNU as
syntax with `.intel_syntax noprefix`, one instruction per line, comments after `#`.

**Checked:**
* the x86-64 SysV and aarch64 listings were assembled and run by the reviewer (2026-11-21, a C
  harness on x86-64 and `llvm-mc` with `+bti` under QEMU for aarch64, scratch files, not
  committed). Seen: the status, `TRAP_CODE`, the alignment the callee sees, the saved block's
  frame word, the exit record put back, and a status with garbage in the upper half;
* the Win64 listing was assembled only (GNU as 2.42); it has not been run on Windows;
* 2026-11-23: compared with `targets/jit/x86_64/entry.py` and `targets/jit/aarch64/entry.py`, the
  instruction order, saves, saved block and landing pads are the same (the xmm saves were
  `movdqa` in the spec's appendix and are `movups` here, as in the code; the block is 16-aligned
  either way). The code the Win64 listing matches runs on the Windows CI runners since S0015 step 1.

**x86-64, SysV host** (`rdi` ctx, `rsi` frame, `rdx` c_entry; `rsp` ≡ 8 mod 16 on entry):
```
endbr64
push rbx                       # rbx keeps ctx across the call (boundary callee-saved)
sub  rsp, 32                   # rsp = 0 mod 16; the saved block is [rsp, rsp + 32)
mov  rbx, rdi
mov  rax, [rbx + EXIT]         # the store's exit record
mov  rcx, [rax]
mov  [rsp], rcx
mov  rcx, [rax + 8]
mov  [rsp + 8], rcx
mov  rcx, [rax + 16]
mov  [rsp + 16], rcx
mov  [rsp + 24], rsi           # the frame
call rdx                       # c_entry(rdi = ctx, rsi = frame); return point = entry point
mov  rcx, [rbx + EXIT]
mov  rdx, [rsp]
mov  [rcx], rdx
mov  rdx, [rsp + 8]
mov  [rcx + 8], rdx
mov  rdx, [rsp + 16]
mov  [rcx + 16], rdx
mov  eax, eax                  # the status is 32 bits
test eax, eax
je   done
mov  [rbx + TRAP], rax
done:
add  rsp, 32
pop  rbx
ret
```
**x86-64, Win64 host** (`rcx` ctx, `rdx` frame, `r8` c_entry; `rsp` ≡ 8 mod 16 on entry). The
callee is SysV, so no shadow space is allocated for it; the host's home area above the return
address is not used:
```
endbr64
push rbx                       # rbx keeps ctx across the call
push rsi                       # Win64 callee-saved, SysV caller-saved
push rdi                       # rsp = 0 mod 16
mov  rdi, rcx                  # the SysV arguments, before anything uses rdi, rsi or rcx: ctx
mov  rsi, rdx                  # frame
sub  rsp, 192                  # the saved block is [rsp, rsp + 32); no shadow space for the SysV callee
movups [rsp + 32], xmm6                # xmm6-xmm15 at [rsp + 32 + 16k]
movups [rsp + 48], xmm7
movups [rsp + 64], xmm8
movups [rsp + 80], xmm9
movups [rsp + 96], xmm10
movups [rsp + 112], xmm11
movups [rsp + 128], xmm12
movups [rsp + 144], xmm13
movups [rsp + 160], xmm14
movups [rsp + 176], xmm15
mov  rbx, rdi
mov  rax, [rbx + EXIT]
mov  rcx, [rax]
mov  [rsp], rcx
mov  rcx, [rax + 8]
mov  [rsp + 8], rcx
mov  rcx, [rax + 16]
mov  [rsp + 16], rcx
mov  [rsp + 24], rsi
call r8                        # c_entry(rdi = ctx, rsi = frame); return point = entry point
mov  rcx, [rbx + EXIT]
mov  rdx, [rsp]
mov  [rcx], rdx
mov  rdx, [rsp + 8]
mov  [rcx + 8], rdx
mov  rdx, [rsp + 16]
mov  [rcx + 16], rdx
mov  eax, eax
test eax, eax
je   done
mov  [rbx + TRAP], rax
done:
movups xmm6, [rsp + 32]
movups xmm7, [rsp + 48]
movups xmm8, [rsp + 64]
movups xmm9, [rsp + 80]
movups xmm10, [rsp + 96]
movups xmm11, [rsp + 112]
movups xmm12, [rsp + 128]
movups xmm13, [rsp + 144]
movups xmm14, [rsp + 160]
movups xmm15, [rsp + 176]
add  rsp, 192
pop  rdi
pop  rsi
pop  rbx
ret
```
**aarch64** (`x0` ctx, `x1` frame, `x2` c_entry; GNU as / `llvm-mc` syntax, comments after `//`;
`bti jc`, as every boundary address, from step 3: a host may also reach it by `br`):
```
bti  jc
stp  x29, x30, [sp, #-128]!
mov  x29, sp
stp  x19, x20, [sp, #16]  // x19 keeps ctx across the call
mov  x19, x0
ldr  x9, [x19, #EXIT]
ldp  x10, x11, [x9]
ldr  x12, [x9, #16]
stp  x10, x11, [sp, #96]  // saved block at [sp + 96, sp + 128)
stp  x12, x1, [sp, #112]
blr  x2  // c_entry(x0 = ctx, x1 = frame); return point = entry point
mov  w0, w0  // the status is 32 bits: zero-extend
ldr  x9, [x19, #EXIT]
ldp  x10, x11, [sp, #96]
ldr  x12, [sp, #112]
stp  x10, x11, [x9]
str  x12, [x9, #16]
cbz  w0, done
str  x0, [x19, #TRAP]
done:
ldp  x19, x20, [sp, #16]
ldp  x29, x30, [sp], #128
ret
```

**Trap codes** (keep the PoC's first six numbers; names are Wasmtime's `TrapCode`):

| code | trap | code | trap |
|---|---|---|---|
| 0 | none | 8 | `BAD_SIGNATURE` |
| 1 | `UNREACHABLE` | 9 | `TABLE_OUT_OF_BOUNDS` |
| 2 | `MEMORY_OUT_OF_BOUNDS` | 10 | `NULL_REFERENCE` |
| 3 | `INTEGER_DIVISION_BY_ZERO` | 11 | `OUT_OF_FUEL` |
| 4 | `INTEGER_OVERFLOW` | 12 | `INTERRUPT` |
| 5 | `STACK_OVERFLOW` | 13 | `HOST_ERROR` (waboros) |
| 6 | `BAD_CONVERSION_TO_INTEGER` | 14 | `UNCAUGHT_EXCEPTION` (inside code: an exception in flight) |
| 7 | `INDIRECT_CALL_TO_NULL` | 15 | `HEAP_MISALIGNED` |
| 16 | `ARRAY_OUT_OF_BOUNDS` (S0013) | 17 | `CAST_FAILURE` (S0013) |
| 18 | `ALLOCATION_TOO_LARGE` (S0013: past Wasmtime's bound for the element size) | | |

Every status with bit 31 set (`0x80000000`–`0xFFFFFFFF`) is **private** to a compiler (S0015): never
assigned here, used only between a compiler's code and its own C entry (`jit-x86_64`: the hop
status `0x80000001`, from S0015 step 1), and never returned by a C entry, the runtime's `entry` or
a bridge.

**Versioning rules**

* `format_version` and `ctx_abi_version` are independent. Loaders accept exactly the versions they
  know; the compiler can emit older versions while hosts migrate.
* Any change here goes with: the version bump, this document, `DECISIONS.md`, and a test that loads an
  artifact of the old version and checks it is refused (or still runs, if supported).
* A small reference loader in C is part of the test suite and runs in CI from phase 5 (D36), to keep the contract honest outside
  Python.
