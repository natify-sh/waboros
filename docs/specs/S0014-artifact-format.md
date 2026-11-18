---
title: JIT artifact format v1 in the ELF64 container
kind: spec
id: S0014
status: approved
superseded_by:
related: [D18, D23, D24, D29, D33, D35, D36, D44, D48, D54, D58, D59, D60, D61, D62, D63, D64, D65, D66, R2, R3, R6, S0003, S0006, S0013]
owner: maintainer
updated: 2026-11-18
---

# S0014 JIT artifact format v1 in the ELF64 container

## Summary
Fixes format version 1 of the JIT artifact ([ARTIFACT.md](../ARTIFACT.md) part 2) in the ELF64
container that D59 chose: the ELF header and section rules, every `.waboros.*` payload byte by
byte, the constant expressions, the canonical type identity, and the loader's rules. A host that
reads this document can instantiate an artifact with **no wasm parser** and without reading our
Python: the waboros runtime, the C reference loader (D36) and the micro-compilers (D60, D68) that
write the format. It also answers the points D59 left to this spec, carries S0006 step 6 (the
guard flag and the fault map in a file), and lands D58's libcall reservation before the format
freezes.

The context ABI (part 3) and the ISA policy (part 1) are not redefined here; this spec refers to
them. Their versions stay 1.

## Goals / Non-goals
* Goals: one byte-exact definition of a v1 file; a self-contained artifact (an inventory of
  everything instantiation reads, each with its section); a deterministic writer (D35: two writes
  and a cross-compiled write are byte-identical); a strict reader that refuses what it does not
  know and never crashes the interpreter on malformed bytes; the D18 fat-file fallback; D62's three
  rules; tests that an old or forged artifact is refused.
* Non-goals: the public API (`Module.serialize`, `Module.deserialize`, `waboros compile`, the
  cache directory: phase 6; this spec gives internal `write` and `read` functions only); the C
  reference loader itself (its own spec, next, D36); statically linked artifacts (D62: an
  amendment after threads or WASI); `pyc` and `mpy` artifacts (Q6); the address map for
  `Trap.frames` (a later optional section, "`.waboros.traps`"); anything that changes code bytes
  except step 0 (D58).
* Rows (D47): no per-OS code. The format is the same on every OS (D23); only the loader's
  executable memory and fault handler differ, and they exist already.

## Precedent: Wasmtime's `.cwasm`
Measured in [ARTIFACT-CONTAINER.md](../ARTIFACT-CONTAINER.md): an ELF64 `ET_REL` with OS/ABI 0xC8,
`e_flags` 0x1 for a native module (0x5 and 0x9 for Pulley), sections `.wasmtime.engine`,
`.text` (4096-aligned), `.wasmtime.addrmap`, `.wasmtime.traps`, `.wasmtime.exceptions`,
`.rodata.wasm` (the data segments, 4096-aligned), `.wasmtime.info`, `.symtab`, `.strtab`,
`.shstrtab`. In Wasmtime's sources (`crates/environ/src/obj.rs` names the sections, the OS/ABI and
the `e_flags` bits for module, component and Pulley; `crates/wasmtime/src/engine/serialization.rs`
writes `.wasmtime.engine`; these paths and the refusal below are from memory, to confirm against the
sources before approval): the engine section holds the Wasmtime version string and the
compile-affecting settings, checked at load, and by default a file from another Wasmtime version is
refused. What we take: the container, private OS/ABI, code in a page-aligned `.text`, metadata
in named sections, a compatibility record checked first. What we do not take: `.wasmtime.info` is a
Rust serde serialization of Wasmtime's own structures, readable only by Wasmtime; our payloads are
specified byte by byte for D36. Wasmtime's per-OS files (D23 gives us one per architecture) and
its target-endian ELF (we are little-endian only, D48) are not taken either.

## Interface

### Conventions
* Every integer is little-endian: `u8`, `u16`, `u32`, `u64`, `s32` (two's complement). No LEB128
  anywhere. Payload fields are packed, not aligned: a C reader reads them with `memcpy`.
* `str`: `u32` byte length, then that many bytes of UTF-8, no terminator.
* `key`: 32 bytes, a SHA-256 digest ("Canonical type key").
* Every payload is parsed **exactly**: a count is checked against the bytes left before anything
  is allocated, every index against its range, and the payload must end where its last field ends.
  Reserved fields are written 0 and a reader refuses a non-zero one.
* Value types are written as in "`.waboros.types`" (`valtype`), so the same five bytes mean the
  same type in every section.

### File layout
**ELF header** (64 bytes; `Elf64_Ehdr`):

| offset | field | value |
|---|---|---|
| 0 | `EI_MAG0..3` | `7F 45 4C 46` |
| 4 | `EI_CLASS` | 2 (`ELFCLASS64`); the only class (D48: no 32-bit JIT) |
| 5 | `EI_DATA` | 1 (`ELFDATA2LSB`); the only byte order (D48) |
| 6 | `EI_VERSION` | 1 |
| 7 | `EI_OSABI` | **0xCB**, private (Q1) |
| 8 | `EI_ABIVERSION` | **`format_version`**: 1 (Q4) |
| 9-15 | `EI_PAD` | 0 |
| 16 | `e_type` u16 | 1 (`ET_REL`): no OS loads it |
| 18 | `e_machine` u16 | 62 `EM_X86_64`, 183 `EM_AARCH64`, 243 `EM_RISCV` (reserved until riscv64 defines its baseline, ARTIFACT.md part 1); replaces part 2's `arch` |
| 20 | `e_version` u32 | 1 |
| 24 | `e_entry` u64 | 0 |
| 32 | `e_phoff` u64 | 0: no program headers |
| 40 | `e_shoff` u64 | offset of the section header table |
| 48 | `e_flags` u32 | 0 (tools read `e_flags` per machine: bit 0 is `EF_RISCV_RVC` to `readelf`, so we keep it empty) |
| 52 | `e_ehsize` u16 | 64 |
| 54 | `e_phentsize` u16 | 0 |
| 56 | `e_phnum` u16 | 0 |
| 58 | `e_shentsize` u16 | 64 |
| 60 | `e_shnum` u16 | number of sections, the null section included |
| 62 | `e_shstrndx` u16 | index of `.shstrtab` (the last section) |

**Sections, in this order:**

| index | name | `sh_type` | `sh_flags` | `sh_addralign` | present |
|---|---|---|---|---|---|
| 0 | (null) | `SHT_NULL` | 0 | 0 | always |
| 1 | `.waboros.header` | `SHT_PROGBITS` (1) | 0 | 8 | always, at file offset 64 |
| 2 | `.text` | `SHT_PROGBITS` | `SHF_ALLOC \| SHF_EXECINSTR` | 4096 | always (may be empty) |
| 3 | `.waboros.types` | `SHT_PROGBITS` | 0 | 8 | always |
| 4 | `.waboros.imports` | `SHT_PROGBITS` | 0 | 8 | always |
| 5 | `.waboros.funcs` | `SHT_PROGBITS` | 0 | 8 | always |
| 6 | `.waboros.tables` | `SHT_PROGBITS` | 0 | 8 | always |
| 7 | `.waboros.memories` | `SHT_PROGBITS` | 0 | 8 | always |
| 8 | `.waboros.globals` | `SHT_PROGBITS` | 0 | 8 | always |
| 9 | `.waboros.tags` | `SHT_PROGBITS` | 0 | 8 | always |
| 10 | `.waboros.exports` | `SHT_PROGBITS` | 0 | 8 | always |
| 11 | `.waboros.segments` | `SHT_PROGBITS` | 0 | 8 | always |
| 12 | `.waboros.context` | `SHT_PROGBITS` | 0 | 8 | always |
| 13 | `.waboros.stackmaps` | `SHT_PROGBITS` | 0 | 8 | always |
| 14 | `.waboros.traps` | `SHT_PROGBITS` | 0 | 8 | always |
| 15 | `.waboros.trampolines` | `SHT_PROGBITS` | 0 | 8 | always |
| 16 | `.waboros.wasm` | `SHT_PROGBITS` | 0 | 8 | fat files only |
| next | `.symtab` | `SHT_SYMTAB` (2) | 0 | 8 | always; `sh_entsize` 24, `sh_link` = `.strtab`, `sh_info` = first non-local symbol |
| next | `.strtab` | `SHT_STRTAB` (3) | 0 | 1 | always |
| last | `.shstrtab` | `SHT_STRTAB` | 0 | 1 | always |

* Every section's data follows the previous one's in the order of the table, at the next offset
  that is a multiple of its `sh_addralign`; the gaps are zero bytes. `.text` therefore starts at
  4096 (the header payload is far smaller than 4032 bytes). The section header table follows the
  last section at the next multiple of 8; the file ends with it.
* `sh_addr` is 0 everywhere; `sh_link`, `sh_info` and `sh_entsize` are 0 except for `.symtab`.
* `START`, `ENTRIES`, `RELOCS` and `CHECKSUM` of ARTIFACT.md part 2 have no section: the start
  function is a field of `.waboros.funcs`; the trampolines of part 2's `ENTRIES` are blobs in
  `.text` found through `.waboros.trampolines` (Q2); no relocation section exists in v1 (the code
  is position-independent); there is no checksum (Q5). Part 2's `FUNCS`, `TRAPMAP`
  and `STACKMAPS` become `.waboros.funcs`, `.waboros.traps` and `.waboros.stackmaps`.

**Symbols** (for tools only: no loader reads `.symtab` or `.strtab`, D62, so their contents are not
part of the contract beyond determinism). Entry 0 is the null symbol. Every other symbol has
`st_other` 0 and `st_shndx` 2 (`.text`). First the locals (`STB_LOCAL`): one `STT_FUNC` per defined
function in index order, named `wasm[<i>]` (`<i>` the function index, imports counted, decimal),
`st_value` its code offset, `st_size` up to the next function's start, the first host thunk or
`data_start`; one `STT_FUNC` per host thunk, named `thunk[<i>]` (`<i>` the imported function's
index); per convention, in the order of `.waboros.trampolines`, `STT_FUNC` symbols
`__waboros_bridge_<conv>`, `__waboros_sp_stub_<conv>` and `__waboros_tail_switch_<conv>` (`<conv>`
`sysv`, `win64`, `aapcs64`); on aarch64 and riscv64 the mapping symbols of AAELF64 and the RISC-V
psABI, `$x` (`STT_NOTYPE`) at 0, `$d` at `data_start` when the data area is not empty, and `$x`
at the first trampoline, so `objdump -d` does not decode jump tables. Then the globals
(`STB_GLOBAL`, `sh_info` points at the first): `__waboros_entry_<conv>`, one `STT_FUNC` per
convention in the same order (D59's names; tools and a future linker only, D62). Each trampoline
symbol's `st_size` reaches the next trampoline's start or the blob's end. Names from the wasm name
section are not in v1 (the front end drops that section); adding them later changes only
`.strtab`, which no loader reads.

**Determinism (D35).** The output is a function of the wasm bytes, the compiler (the `compiler`
string, below) and the code-affecting options only: the order of sections and symbols is fixed as
above, string tables are in first-use order (`.shstrtab` in section order, `.strtab` in symbol
order), padding is zero, nothing records a time, a path, a host name or the compiling machine.
A write on x86-64 and on arm64 of the same module for the same target gives the same bytes.

**Frozen across format versions (D18)** so that every future loader can fall back on a fat file of
any version: the ELF identification bytes 0-8, the meaning of `e_shoff`, `e_shentsize`, `e_shnum`
and `e_shstrndx` (ELF's own), the section names `.waboros.header` and `.waboros.wasm`, the first 32
bytes of `.waboros.header` (`module_sha256`) and the content of `.waboros.wasm` (the wasm bytes,
unchanged). Everything else may change with `format_version`.

### `.waboros.header`

| offset | type | field |
|---|---|---|
| 0 | `key` | `module_sha256`: SHA-256 of the exact wasm bytes compiled. Frozen |
| 32 | `key` | `config_sha256`: SHA-256 of the code-affecting options (below) |
| 64 | u16 | `ctx_abi_version`: 1 (part 3) |
| 66 | u16 | reserved, 0 |
| 68 | u16 | `isa_level`: per `e_machine`; x86-64: 1 = x86-64-v1 (baseline), 2 = v2, 3 = v3; aarch64: 1 = ARMv8.0-A with FP and Advanced SIMD |
| 70 | u16 | reserved, 0 |
| 72 | u32 | `flags`: code-affecting options. Bit 0 `BOUNDS_GUARD`, bits 8-15 `GUARD_LOG2` (S0006: 25 for 32 MiB; 0 when bit 0 is clear). No other bit is defined in v1 |
| 76 | u32 | reserved, 0 |
| 80 | u64 | `isa_features`: features above `isa_level` (D24, D33); the registry per architecture is empty in v1, so v1 writers write 0 |
| 88 | u64 | `features_required`: format amendments a loader must understand (D66; registry empty in v1) |
| 96 | u64 | `features_optional`: format amendments a loader may ignore (registry empty in v1) |
| 104 | `str` | `compiler`: `waboros <version> <target> <target version>`, four tokens separated by one space, ASCII, e.g. `waboros 0.0.0 jit-x86_64 0`; `<version>` is the package version, `<target>` the compiler target that D64 keys the part-3 "Per architecture" table by, `<target version>` the target's `version` |

The payload ends after `compiler`.

**Must understand.** Every defined bit of `flags` and `isa_features`, and every bit of
`features_required`, must be known to the loader: an unknown one is a refusal (thin) or a
regeneration (fat). Unknown bits of `features_optional` are ignored. An amendment inside v1 that an
older loader would misread takes a `features_required` bit; one it can safely ignore (a new
optional section needs no bit at all, as unknown sections are skipped) takes a `features_optional`
bit only if a reader benefits from knowing it is there. A new bit is added by a dated line in this
spec's Changes and a row in ARTIFACT.md; bits are never reused.

**`config_sha256`** is SHA-256 of UTF-8 lines `name=value\n` sorted by name, one per code-affecting
option. v1 names: `bounds` (`guard` or `explicit`), `guard_log2` (decimal, `0` for explicit),
`isa` (the level name, e.g. `x86-64-v1`, then `+feature` per feature in registry order), `target`
(the compiler target). It is a cache key (D29, D64), not a check: a loader decides from the header
fields, never from this hash. A new code-affecting option adds a name, and a value equal to the
option's default is omitted, so existing hashes do not change.

### `.text`
The machine code of the module, then its data area (jump tables of 32-bit relative offsets, never
executed) from `data_start` (`.waboros.funcs`) to the first trampoline: together exactly the bytes
the backend's `finish` returns. Then the trampoline blob of each convention (`.waboros.trampolines`),
each at the next offset that is a multiple of 16, the gaps zero bytes; each blob is exactly what
the architecture's `abi.assemble` returns for that convention. Position-independent: branches are
relative (the trampolines' too: aarch64's `tail_switch` uses `adr` within its blob) and everything
absolute is reached through the context register (part 3). A loader copies it into executable memory whose
start is a multiple of 4096 (aarch64's `adrp` rule, part 3 "Per architecture"), applies W^X and,
on aarch64, invalidates the instruction cache. Nothing is patched.

### `.waboros.types`

```
u32 ngroups
ngroups x group:   u32 nmembers (>= 1), nmembers x subtype
subtype:           u8 final (0 or 1), u8 nsuper (0 or 1), u16 0, [u32 super type index], comptype
comptype:          u8 form, then
                     0x60 func:   u32 np, np x valtype, u32 nr, nr x valtype
                     0x5F struct: u32 nf, nf x field
                     0x5E array:  field
field:             storage (u8 0x78 i8 | u8 0x77 i16 | valtype), u8 mutable (0 or 1)
valtype:           u8 0x7F i32 | 0x7E i64 | 0x7D f32 | 0x7C f64 | 0x7B v128
                   | u8 0x63 (ref null) or 0x64 (ref), s32 heap
heap:              >= 0: a type index; < 0: an abstract heap type, the wasm byte minus 128:
                   -16 func, -17 extern, -18 any, -19 eq, -20 i31, -21 struct, -22 array,
                   -15 none, -14 noextern, -13 nofunc, -23 exn, -12 noexn
u32 ntypes         (= the sum of nmembers)
ntypes x key       the canonical key of each type, in index order
```

Type indices count the members of the groups in order (the wasm type index space). A shorthand
reference type (`funcref`, `externref`, `anyref`, ...) is written in its long form (`0x63`, its
abstract heap type), so equal types have equal bytes. A module without GC types writes each
function type as a group of one, `final` 1, no supertype. The reader checks every type index
against `ntypes`, a supertype index against the type's own index (it must be smaller), and every
form, byte and flag against the lists above.

**Canonical type key.** Two types of any modules are equivalent (iso-recursive equivalence of Wasm
3.0) exactly when their keys are equal. The key of the type at position `k` of its group is

```
key = SHA-256( CG || u32 k )
CG  = u32 nmembers, then per member: u8 final, u8 nsuper, [ref(super)], comptype'
```

where `comptype'` is the member's `comptype` with every `heap` replaced by `u8 0, s32 code` for an
abstract heap type or `ref(x)` for a type index `x`, and `ref(x)` is `u8 1, u32 (x - group start)`
when `x` is in the same group, else `u8 2, key(x)` (a type outside the group is always earlier, so
keys are computed in index order, with no recursion). Every other byte is as in the section above.
The writer stores the keys; a loader may recompute them to check, and must use the stored ones to
match imports (by key, and declared subtyping by following supertype indices and comparing keys,
as `link.py` does), to number function types (the process- or store-wide canonical type ids of
part 3) and to key type-info records in the GC heap (part 3; an exception record keys `"exn "`
followed by the 32 key bytes).

This replaces the key in `frontend/types.py`, which digests Python's `repr` of nested tuples: the
same equivalence, but no host outside Python can compute it, and the micro-compilers (D60) must
write it. Keys never reach code bytes (type ids are context words written at instantiation), so
the change moves no code byte (step 1).

### `.waboros.imports`

```
u32 n
n x import:  str module, str name, u8 kind, desc
  kind 0 func:   u32 type index (a function type)
  kind 1 table:  valtype (a reference type), limits
  kind 2 memory: limits
  kind 3 global: valtype, u8 mutable
  kind 4 tag:    u32 type index (a function type)
limits:       u8 flags (bit 0 max present, bit 1 64-bit addresses, bit 2 shared; other bits 0),
              u8 page_log2 (a memory: 16; a table: 0), u16 0, u64 min, u64 max (0 when absent)
```

Imports in module order. A shared memory (bit 2) is never written by v1 writers (the front end
refuses shared limits); a loader that does not support threads refuses it (D58 keeps the bit and
the size rule for the threads amendment). `page_log2` other than 16 is refused (custom page sizes
are not supported).

### `.waboros.funcs`

```
u32 data_start   offset in .text of the data area, which runs to the first trampoline (its zero
                 padding included); when the area is empty, the end of the code
u32 start        the start function's index (imports counted), 0xFFFFFFFF for none
u32 n            defined functions
n x func:        u32 type index, u32 code offset
```

The code offset is where the entry calls the function (part 3, `entry(ctx, frame, code)`); it is
below `data_start`, and offsets are strictly increasing. The number of result slots, which the host
reads after a call, follows from the type: one slot per result, two for a `v128`.

### `.waboros.tables`, `.waboros.memories`, `.waboros.globals`, `.waboros.tags`, `.waboros.exports`

```
tables:    u32 n, n x (valtype (a reference type), limits, u8 has_init, [cexpr init])
memories:  u32 n, n x limits
globals:   u32 n, n x (valtype, u8 mutable, cexpr init)
tags:      u32 n, n x u32 type index (a function type)
exports:   u32 n, n x (str name, u8 kind (0 func, 1 table, 2 memory, 3 global, 4 tag), u32 index)
```

Defined entities only (imports are in `.waboros.imports`, and each index space counts its imports
first, as in wasm). Exports in module order; an index counts imports. A table without `init`
starts with null elements.

### `.waboros.segments`

```
u32 nelem
nelem x elem:  u8 mode (0 active, 1 passive, 2 declarative), valtype (the segment's reference type),
               [active: u32 table index, cexpr offset],
               u8 items (0: function indices, 1: expressions), u32 count,
               count x u32 function index   | count x cexpr
u32 ndata
ndata x data:  u8 mode (0 active, 1 passive), [active: u32 memory index, cexpr offset],
               u32 length, length bytes
```

`items` is 0 exactly when every item is a plain `ref.func` (each then means the function's record,
as `ref.func`); the writer chooses it by that rule only, so the bytes are deterministic. The
segment's reference type is kept because the collector reads it (passive segments of any-, extern-
or exn-hierarchy references are roots, D61).

### Constant expressions (`cexpr`)

```
u32 length     bytes of the operations that follow
operations:    u16 code, then its immediates, until length is used up
```

| code | operation | immediates | result |
|---|---|---|---|
| 0x0041 | `i32.const` | u32 | the value |
| 0x0042 | `i64.const` | u64 | the value |
| 0x0043 | `f32.const` | u32 | the bit pattern |
| 0x0044 | `f64.const` | u64 | the bit pattern |
| 0xFD0C | `v128.const` | 16 bytes | the 128-bit value, byte 0 lowest |
| 0x0023 | `global.get` | u32 global index | the global's current value (an imported one through the pointer in its context word) |
| 0x00D2 | `ref.func` | u32 function index | the address of the function's record (the index must be in `.waboros.context` records) |
| 0x00D0 | `ref.null` | none | 0 |
| 0x006A, 0x006B, 0x006C | `i32.add`, `sub`, `mul` | none | modulo 2³² |
| 0x007C, 0x007D, 0x007E | `i64.add`, `sub`, `mul` | none | modulo 2⁶⁴ |
| 0xFB1C | `ref.i31` | none | `(v << 1 \| 1)` modulo 2³² (part 3's reference word) |
| 0xFB1A, 0xFB1B | `any.convert_extern`, `extern.convert_any` | none | the operand (one word encoding for both hierarchies, part 3) |
| 0xFB00 | `struct.new` | u32 type index (a struct) | pops one value per field, the first field deepest; a new object (part 3, "Objects") |
| 0xFB01 | `struct.new_default` | u32 type index | a new object with zero fields |
| 0xFB06 | `array.new` | u32 type index (an array) | pops the value, then the length on top; a new array |
| 0xFB07 | `array.new_default` | u32 type index | pops the length; zero elements |
| 0xFB08 | `array.new_fixed` | u32 type index, u32 n | pops n values, the first element deepest |

The codes are the wasm opcodes (a prefixed one as `prefix << 8 | sub-opcode`), so the table reads
as the wasm constant-expression subset of Wasm 3.0 (extended constants and GC included); the
immediates are fixed-width. Evaluation is a stack machine over values of up to 128 bits, run
without recursion; it must end with exactly one value. The reader checks each code and immediate,
each index's range and kind, and the stack depth (no underflow, one value at the end) while it
parses. An allocation follows part 3 ("The GC heap": the type-info record of the type, fields at
their storage sizes, `ALLOCATION_TOO_LARGE` past the element-size bound) and never collects (D61's
deviation for instantiation). The writer emits the validated expression unchanged: no folding.

Why this form (Q9): values that depend on the instance (`global.get` of an import, `ref.func`'s
record address, a GC allocation) cannot be precomputed, so some evaluator is needed anyway; the
wasm bytes themselves would need LEB128, s33 heap types, prefix decoding and the GC immediate forms
in every loader. A fixed-width subset with wasm's own opcode numbers costs one table and keeps the
loader's job to "read, check, evaluate".

### `.waboros.context`
What the context layout (part 3) and instantiation need beyond the counts of the sections above;
in the compiler these are `Compiled.records`, `sigs`, `thunks`, `rtts`, `thrown` and the heap and
subtype decisions, which are computed from the function bodies, so a loader cannot recompute them.

```
u8  heap        1: the module has the GC heap's two words and the GC group (gc.needs_heap)
u8  subtype     1: the code has a function-subtype site; the IS_SUBTYPE record must be written
u16 0
u32 nrecords,  nrecords x u32 function index   every function with a record (strictly increasing;
                                               includes every function import)
u32 nsigs,     nsigs x u32 type index          one signature word each (strictly increasing)
u32 nthunks,   nthunks x u32 code offset       the host thunk of each function import, in import
                                               order (nthunks = the number of function imports)
u32 nrtts,     nrtts x u32 type index          one RTT word each (struct or array types,
                                               strictly increasing)
u32 nthrown,   nthrown x u32 tag index         one RTT word each, after those (strictly increasing)
```

`heap` 0 requires `subtype`, `nrtts` and `nthrown` 0. The context's words then follow from
part 3 in its order: memories = imported + defined; the heap's two words when `heap`; tables =
imported + defined; globals = imported + defined, two words for each **defined** `v128` global;
function imports; the libcall block; `nsigs` signature words; `nrecords` records of 3 words; tags =
imported + defined; when `heap`, the 8 GC service words, `nrtts + nthrown` RTT words and the
3-word IS_SUBTYPE record; at least 18 words in total (part 3: the entry reads memory 0's words).
When `heap`, the loader also writes a function-type record for every function type of the module
that has a supertype or is not final (part 3, "Type-info records").

### `.waboros.stackmaps`
The `STACKMAPS` payload of ARTIFACT.md part 2, unchanged (S0013, D65): `u32 nsites`, the 16-byte
site records sorted by `ret`, `u32 nmaps`, the pool of `refs` and `live` bitmaps. Required; a
loader decodes it with the checks listed there and refuses one that does not parse exactly.

### `.waboros.traps`

```
u32 n, n x (u32 fault_offset, u32 resume_offset)   fault_offset strictly increasing
```

The guard fault map of S0006 ("Artifact", the handler contract): `n` is 0 unless `BOUNDS_GUARD`
is set, every offset is below `data_start`. This section holds the fault map only. The address
map for `Trap.frames` (code offset to wasm function and byte offset), which part 2 reserved in
`TRAPMAP` after the fault map, is not produced by the compiler; when it is, it is a separate
optional section `.waboros.addrmap` (Wasmtime's `.wasmtime.addrmap`), which older loaders skip, so
it needs no version and no flag. Appending it to `.waboros.traps` would break the exact parse.

### `.waboros.wasm`
The exact wasm bytes whose SHA-256 is `module_sha256`. Its presence makes a **fat** file (D18).

### `.waboros.trampolines` (Q2)
The entry, the bridge, the stack-pointer stub and `tail_switch` of part 3, for every host
convention of the architecture, are in every module artifact (part 2's `ENTRIES`, D59; D23: one artifact for
every OS, so x86-64 carries both conventions):

```
u32 n
n x convention:  u8 id (1 sysv, 2 win64, 3 aapcs64), u8 0, u16 0,
                 u32 start, u32 size,                                  the blob in .text
                 u32 entry, u32 bridge, u32 sp_stub, u32 tail_switch,  offsets in .text
                 u32 entry_ret, u32 restore,                           the walk's points, in .text
                 u32 saved                                             ENTRY_SAVED (part 3)
```

x86-64 writes `sysv` then `win64`; aarch64 writes `aapcs64` (one convention on Linux, macOS and
Windows); riscv64 defines its own with its baseline. Every offset lies inside its blob; `start` of
the first blob is the end of the data area. Sizes today (2026-11-17, `abi.assemble`): x86-64 308
bytes (`sysv`) and 314 (`win64`), aarch64 316, so about 0.6 KB per x86-64 artifact and 0.3 KB per
aarch64 one. A loader finds them only through this payload, never through a symbol (D62).

**One copy per store.** The frame walk recognises the entry's return point and `tail_switch`'s
`restore` by address, and `tail_switch` checks for its own `restore`, so every instance of a store
runs on one copy of the trampolines of the host's convention (part 3; D64 item 5 allows one copy per
compiler, shared by stores). That copy is the store's (a foreign host: the first module artifact
loaded into the store provides it; the waboros runtime: the copy it assembles in the process, below).
Every later artifact's blob for that convention, and its `entry_ret`, `restore` and `saved`
relative to the blob's `start`, **must be byte-identical** to the store's copy. The loader checks
this (a compare of about 300 bytes), and does not rely on the `compiler` check alone: equal
`compiler` strings are the writer's promise, the compare is the proof, and its failure means a
corrupt or forged file. A mismatch is a refusal (thin) or a regeneration (fat). The other
conventions' blobs are checked for being well-formed (offsets inside the blob), not compared.

**The waboros runtime** keeps assembling its trampolines in the process (`target.trampolines()`,
one per process and architecture) and runs every store on them; it never executes an artifact's
copy. Its check is the one above with its own copy as the store's: each artifact's blob for the
running convention must equal the bytes `abi.assemble` gives, so the in-process and the file copies
cannot drift. The writer writes exactly `abi.assemble`'s bytes for every convention.

The collector module (D61) is an ordinary module artifact compiled from
`runtime/gc/collector.wasm`.

### What instantiation reads: inventory
Every field `JitInstance.__init__`, `Instance`, `JitFunc`, `link.py`, `heap.py`, `gc.py` and
`collect.py` read today from `Compiled` and from `Compiled.module` (the `ValidModule`), and where
the artifact carries it. The reader returns the same `Compiled` with a module view that has exactly
the module keys below (no `codes`, `insns` or masks), so the instance code stays one path for
compiled and loaded modules.

| read today | by | carried in |
|---|---|---|
| `tdefs` (composite, supertypes, final, rec group), `types` (function signatures; `([], [])` for struct and array) | `types.key`, `link.py` matching, `heap.rtt` and layouts, `gc.storage_size`, `_root`, `reftypes`, `type_id` supertypes, function-type records | `.waboros.types` |
| `tkeys` (the key cache) | `types.key` | `.waboros.types` keys |
| `imports` (kind, module, name, desc) | instance, `link.py` | `.waboros.imports` |
| `ttypes`, `taddr` (every table, imports first) | instance (`Table.refs`), `link.py` | imports + `.waboros.tables` |
| `maddr` (every memory, imports first), `mems` | instance, `link.py` | imports + `.waboros.memories` |
| `funcs` (type index per defined function) | `ftypes`, `link.py`, `JitFunc.record` | `.waboros.funcs` |
| `tables`, `tinit` | instance | `.waboros.tables` |
| `globals` (type, mutability, init) | instance, `link.py`, the collector's roots | `.waboros.globals` |
| `tags` | instance, `link.py` | `.waboros.tags` |
| `exports` | `export`, `limits`, `link.py`, the collector's `is_subtype` index | `.waboros.exports` |
| `start` | instance | `.waboros.funcs` |
| `elem_info` (table, offset, reference type, items), `elem_mode` | instance, `collect.py` | `.waboros.segments` |
| `datas`, `data_mem` | instance | `.waboros.segments` |
| `gc.subtype_sites(mod)` (scans the function bodies at instantiation) | instance | `.waboros.context` `subtype` |
| `Compiled.code`, `data_start` | `Instance`, tools | `.text`, `.waboros.funcs` |
| `Compiled.funcs` (code offset, result slots) | `Instance.call`, `JitFunc.record` | `.waboros.funcs` (slots from the type) |
| `Compiled.layout` | `Instance` | derived ("`.waboros.context`") |
| `records`, `sigs`, `thunks`, `rtts`, `thrown` | instance | `.waboros.context` |
| `flags`, `faults` | `target.instantiate`, instance | header, `.waboros.traps` |
| `stackmaps` | instance, walk, collector | `.waboros.stackmaps` |
| `Trampolines` (`target.trampolines()`: entry, bridge, sp_stub, tail_switch, entry_ret, restore, saved) | `Instance`, the walk, `tail_switch` | `.text` + `.waboros.trampolines` (one copy per store, checked) |
| `refmasks`, `v128masks`, `insns`, `codes`, `validated`, `gc`, `anyext` | the compiler only | not carried |

## Semantics: the loader

**Trust.** An artifact is native code: loading one from an untrusted source runs untrusted code,
as Wasmtime's `Module::deserialize` is `unsafe`. The reader's checks are against truncation,
corruption, other versions and forged *formats*: a malformed file is refused with a message and
never crashes the interpreter; they are not a sandbox.

**Order of checks** (each failure names its reason; "refuse" means a thin file is refused and a
fat file is regenerated, below):
1. Identification: `EI_MAG`, `EI_CLASS` 2, `EI_DATA` 1, `EI_VERSION` 1, `EI_OSABI` 0xCB. Otherwise
   "not a waboros artifact": no fallback, the file is not ours.
2. `EI_ABIVERSION` is a `format_version` the loader reads (v1 loaders: 1). Otherwise refuse.
3. The ELF header fields of the table above, the section header table inside the file, every
   section inside the file, names unique, every required section present, no `SHT_REL` or
   `SHT_RELA` section (v1 has none; one would mean code the loader cannot place). Sections with
   other names are skipped (D62; `.note.GNU-stack` or a later descriptor section need no version).
   `sh_flags` of `.waboros.*` sections are ignored; sections are found by name (D62).
4. `.waboros.header` parses exactly.
5. Compatibility: `e_machine` is the running architecture (a cross-compiled artifact is never run on
   the compiling host, D35); `ctx_abi_version` is 1; the `compiler` target is the store's compiler
   (D64) and its versions are acceptable (Q3); `isa_level` and every `isa_features` bit are known
   and present on the CPU (part 1; the message names the missing features); every `flags` bit is
   known; `BOUNDS_GUARD` only where this host can run guard code (S0006: a handler for its row,
   installed and ours, and a guard at least `2^GUARD_LOG2`); every `features_required` bit is known.
6. Every payload parses exactly with its checks; the trampolines of the host's convention equal
   the store's copy ("`.waboros.trampolines`").
7. Instantiation, in the order of the wasm specification's instantiation: imports matched (kind,
   type by key and declared subtyping, limits and address type, as `link.py`); the context written
   (part 3); type-info records when `heap`; records, signature words, tags; globals in order
   (each `cexpr`); tables (each `init` or null); element items of every segment; active element
   segments in order (offset, bounds, write; then the segment is dropped, as are declarative ones);
   active data segments in order (`MEMORY_OUT_OF_BOUNDS` past the memory, writes before it stay);
   the start function.

**Fat files (D18).** A fat file is never refused for any reason in steps 2-6: the loader finds
`.waboros.wasm` through the frozen part, checks SHA-256 of its bytes against `module_sha256`
(a mismatch is a refusal: the file is corrupt), and compiles the wasm with the store's compiler
and the loader's own options (it may cache the result). A thin file is refused at the first
failing check. Foreign hosts (D36) refuse in both cases: they have no compiler.

**D62's three rules hold in v1:** `sh_flags` of `.waboros.*` ignored and sections found by name;
no loader depends on a symbol name (functions through `.waboros.funcs`, trampolines through
`.waboros.trampolines`); unknown sections skipped.

**The writer** takes the compiled module, the wasm bytes when fat, and the options as explicit
inputs. It refuses (`ValueError`) to write when a test switch that changes code bytes or maps is set
(`WABOROS_JIT_NOPIN`, `WABOROS_JIT_NOLIVE`, `WABOROS_JIT_PAD`, `WABOROS_JIT_LOOP_ALIGN`; Q11), so
none reaches a file. The bounds mode is an input, not the compiling host's `auto` (Q12), so a
cross-compiled file does not depend on the host that wrote it.

## Decisions on D59's open list
Decided by the maintainer on 2026-11-17; the open questions below keep the options and costs.

| D59 point | this spec | question |
|---|---|---|
| `EI_OSABI` | 0xCB | Q1 |
| where `format_version` lives | `EI_ABIVERSION` only; the header has no copy | Q4 |
| little-endian and ELF64 only | yes (D48) | — |
| the header split | `.waboros.header` as above; D18's frozen part as above | — |
| `CHECKSUM` | dropped | Q5 |
| `RELOCS` | none; `SHT_REL`/`SHT_RELA` refused in v1; a relocating amendment (copy-and-patch, static builds D62) takes a `features_required` bit | — |
| `pyc` / `mpy` artifacts | not in v1; later `EM_NONE` in the same container | Q6 |
| file extension | `.wbo` | Q7 |
| feature-flag set (D66) | `features_required` and `features_optional`, both empty in v1 | Q8 |
| S0006 step 6 | `flags` bits and `.waboros.traps` as above; the C loader's handler goes to the C loader's spec | — |

File names: `<name>.<target>-<level>[-guard].wbo`, e.g. `core.jit-x86_64-v1.wbo`,
`core.jit-x86_64-v1-guard.wbo`, `core.jit-aarch64-v1.wbo`. The header, not the name, is
authoritative (D18).

## Test plan
* **Round trip** (`tests/artifact/test_roundtrip.py`): for every module of the pinned spec suite
  that `jit-x86_64` and `jit-aarch64` compile (aarch64 cross-compiled on x86-64, D35), in both
  bounds modes, and for CoreMark: `read(write(c))` gives every field of the inventory equal to
  `c`'s, and `write(read(b)) == b`; with `wasm=` the same plus the fat section.
* **Trampolines**: in every written artifact, each convention's blob and offsets equal
  `abi.assemble`'s for that convention (both x86-64 conventions on any host); an instance loaded
  from a file runs on the process's copy, and a file whose running-convention blob differs by one
  byte (or whose `restore` is moved) is refused thin and regenerated fat; in a C-style test of the
  store rule, two artifacts loaded into one store share the first one's copy and a cross-instance
  tail call and a walk across both work.
* **The spec suite through files**: `WABOROS_JIT_ARTIFACT=1 uv run python -m tests.spec.run
  --engine jit-x86_64` (every compiled module written, read back and instantiated from the read
  form, with no `ValidModule` behind it), both bounds modes, with `WABOROS_JIT_WALK_CHECK=1` and
  the GC switches for `gc/` and `exceptions/`; then `uv run python tools/spec_diff.py jit-x86_64
  <the same with the switch>` gives no difference. The same for `jit-aarch64` under QEMU and on
  the native arm64 runner.
* **CoreMark through a file** on both JITs (`tools/run_coremark_jit.py` with the switch): the self
  check passes; run time alternated with the direct path (no change expected: the code is the
  same bytes); write time, read time, instantiation time and file size recorded in BENCHMARKS.md.
* **Determinism (D35)**: each module written twice in one process and in two processes gives the
  same bytes; in CI's arm64 job, the files written there for `jit-aarch64` equal the files written
  on x86-64 (`tools/jit_dump.py` gains a mode that hashes artifact files) and the x86-64-written
  file runs there.
* **Refusals** (`tests/artifact/test_refuse.py`), each with its message, each thin and fat (fat:
  regenerated and run): wrong magic or OS/ABI (refused even fat); `EI_ABIVERSION` 0 and 2;
  `e_machine` of the other architecture; `ctx_abi_version` 2; `isa_level` 3 on a CPU without AVX2
  and an unknown `isa_features` bit; an unknown `flags` bit; `BOUNDS_GUARD` where guard is
  unavailable (the row forced) and `GUARD_LOG2` larger than the host's; an unknown
  `features_required` bit (refused) and an unknown `features_optional` bit (accepted); another
  compiler target and another compiler version (Q3); a `SHT_RELA` section; a missing required
  section; a duplicated section; a non-zero reserved field; trailing bytes in each payload; an index
  out of range in each payload; every truncation of a small artifact (each prefix length) refused,
  never a crash; a fat file whose wasm does not match `module_sha256`. An unknown section
  (`.waboros.future`, `.note.GNU-stack`) is skipped and the module runs.
* **An old artifact** (CLAUDE.md, "The JIT artifact is a contract"): a v1 file per JIT committed
  under `tests/artifact/data/` (small: an exported add, a memory, a table, a global, a GC struct
  global); every later format or ABI change must keep its test (the file runs, or is refused with
  the version message).
* **Independent readers, CI only** (development tools, never imported by the package): `readelf
  -aW` and `llvm-readobj --all` print no warning on every artifact of the round-trip set;
  pyelftools reads the same sections, offsets and symbols as our reader; `llvm-objdump -d` lists
  every function by its symbol; `eu-elflint --gnu-ld` runs with its known complaints about a
  private OS/ABI recorded (step 6 lists them). `tools/isa_all.py` reads the files (the code up to
  `data_start` and every trampoline blob) instead of `WABOROS_JIT_DUMP` blobs.
* **Type key**: for every pair of types in the spec suite's modules (and across modules of one
  `.wast`), the new key is equal exactly when the old `repr` key is equal (the old function kept
  in the test); `linking`, `type-canon`, `type-equivalence`, `type-rec`, `gc/type-subtyping` and the rest of
  `gc/` unchanged on
  `pyc` and both JITs.
* `uv run pytest -q`, `python3 tools/check_docs.py`, `python3 tools/features.py --check`.

## Plan of steps
The format is a contract: the reader and the instance change are read by a `reviewer` (step 8).
Each step stops and reports if it needs more than its size.

| step | goal | agent | acceptance | size (lines) |
|---|---|---|---|---|
| 0 | D58: the libcall block is 10 words (6 used, 4 zero) on both JITs; part 3 and S0003 updated | jit-engineer | spec suite on both JITs, both bounds modes, at parity with before; ISA gate; D35 lists regenerated and the cross-compiled identity holds; CoreMark alternated within the spread | src ~20, tests ~20 |
| 1 | The canonical type key in bytes (`frontend/types.py`), keys stored as 32 bytes | implementer | the type-key test; `linking`, `type-*` and `gc/` unchanged on `pyc` and both JITs; code bytes unchanged (D35 lists) | src ~80, tests ~60 |
| 2 | Writer and reader: the ELF container and every payload, the module view, `cexpr` both ways | implementer | round trip on every spec module and CoreMark, both JITs, both modes; written twice, identical | src ~550, tests ~250 |
| 3 | Instantiate from the read form: the instance takes the module view and `subtype` from the artifact; the `WABOROS_JIT_ARTIFACT=1` switch | jit-engineer | the spec suite through files at parity, both JITs, both modes, walk check and GC switches; CoreMark through a file; timings recorded | src ~120, tests ~80 |
| 4 | Loader rules: the checks in order, fat fallback, the refusal tests, the committed v1 files | implementer | the refusal and old-artifact tests; `pytest -q` | src ~120, tests ~300 |
| 5 | Trampolines in every artifact (Q2): the blobs in `.text`, `.waboros.trampolines`, the symbols, the store's identity check in the loader | jit-engineer | the trampoline tests (equal to `abi.assemble`, mismatch refused or regenerated, the store rule); the round trip and the spec suite through files still at parity; D35 identity of files | src ~90, tests ~80 |
| 6 | D35 on files and the independent readers in CI; `isa_all.py` over files | runner | the CI jobs green; the identity holds on the arm64 runner | tools and CI ~120 |
| 7 | Docs ("Docs this changes"), DECISIONS entry | orchestrator | `python3 tools/check_docs.py` | — |
| 8 | Review of steps 0-5 without the implementers' reasoning: the reader's checks (truncation, ranges, exact parse), the frozen part, the refusals, `cexpr` evaluation, determinism | reviewer | findings closed | — |

About 1 000 lines of source and 800 of tests in total. The C reference loader (D36) follows in its
own spec and reads this format as it stands after step 8.

**Size, from measured parts** (CoreMark, `jit-x86_64` in guard mode, 2026-11-17, this repository's
`gc` branch): `.text` 34 547 bytes (data area from 34 023), 669 fault points (5 356 bytes of
`.waboros.traps`), `.waboros.stackmaps` 1 736 bytes, data segments 1 932 bytes, wasm 19 463 bytes.
With the 4 KiB before `.text`, the trampolines (622 bytes), the symbols and the section headers
(about 2 KB), a thin file should come to about 51 KB and a fat one to about 70 KB (Wasmtime's `.cwasm` of the same module: 53 608
bytes); step 2 measures it.

## Alternatives considered
* **Our own container** (part 2 as written) and **our own plus an ELF exporter**: rejected by D59.
* **A per-section version**: rejected by D66.
* **A generic serialization** of the compiler's structures (Wasmtime's `.wasmtime.info` is serde
  data of Rust types): readable only by the writer's language; D36 needs byte-level payloads.
* **Precomputed type-info layouts** (size, field offsets, traced offsets per type) in the
  artifact: saves a loader the part-3 layout rule, but records are shared by key across a store's
  modules, so two artifacts that disagree for one key would corrupt the heap; the rule in part 3 is
  the single source and the loader computes it.
* **A separate trampoline artifact per compiler** (one file for foreign hosts, none in module
  artifacts) and **no trampolines in any artifact** (foreign hosts write their own from part 3):
  rejected in Q2.
* **wasm constant-expression bytes as they are**, or **precomputed values only**: see Q9.
* **A `.waboros.data` section aligned to 4096** for copy-on-write memory images (Wasmtime's
  `.rodata.wasm`): no measurement asks for it; a later writer may align the data differently
  without a version, since readers find every byte through lengths, not alignment.

## Open questions
1. **`EI_OSABI` value.** (a) 0xCB, as the prototype (checked with `readelf`, `objdump`,
   `llvm-objdump`, `llvm-readobj` and pyelftools without a warning, D59); (b) 0x57 (`W`); (c) any
   other value of 64-254 not used by a known toolchain. Values 64-66 are taken by ARM, C6000 and
   AMDGPU, 97 and 255 by `ELFOSABI_ARM` and `ELFOSABI_STANDALONE`, 0xC8 by Wasmtime. Cost: none
   either way. Recommendation: **(a) 0xCB**. **Decided (maintainer, 2026-11-17): (a) 0xCB.**
2. **Where the entry trampolines live.** (a) A separate trampoline artifact per compiler (told apart by a
   `kind` field in the header), the module artifacts without any: a store uses one copy, as the walk needs; one more
   file for foreign hosts. (b) In every module artifact (part 2's `ENTRIES`, D59's symbols): one
   file, but each instance would carry its own copy, and the walk and `tail_switch`'s restore check
   rely on one copy per store, so the loader must still pick one, and every artifact grows by
   about 0.3 KB per convention. (c) In no artifact: a foreign host writes its own from part 3's
   tables: no file, but every host language reimplements the trampolines. Recommendation was (a).
   **Decided (maintainer, 2026-11-17): (b), in every module artifact** (part 2's `ENTRIES`, D59's
   symbols): `.waboros.trampolines` and the blobs in `.text`; a store runs on one copy, and the
   loader checks every later artifact's copy byte for byte against it.
3. **Which versions the waboros runtime accepts in a thin file.** (a) Only its own `compiler`
   string, exactly (D18 as written; Wasmtime's default): a new waboros release refuses older thin
   files, the cache recompiles. (b) Any file whose `format_version`, `ctx_abi_version`, target and
   target version match, minus a known-bad list (part 2 mentions one): files survive upgrades, but
   every release must keep v1 instantiation correct for files written by older compilers, and the
   tests must cover them. Recommendation: **(a)** now; (b) later by its own decision, when a release
   ships artifacts. Foreign hosts (D36) check the contract fields and the target only.
   **Decided (maintainer, 2026-11-17): (a), the exact `compiler` string.**
4. **Where `format_version` lives.** (a) `EI_ABIVERSION` (one byte, in the ELF header, which is
   frozen anyway; refused before any section is read); (b) a u16 in `.waboros.header` with
   `EI_ABIVERSION` 0. Not both: one source of truth (D59). 255 versions suffice. Recommendation:
   **(a)**. **Decided (maintainer, 2026-11-17): (a), `EI_ABIVERSION` only.**
5. **Checksum.** (a) Drop it: the strict reader refuses truncated and malformed files,
   `module_sha256` checks the wasm of a fat file, a checksum is no defence against a forged file
   (the file is code), and Wasmtime has none; cache writes must be atomic (write and rename), which
   phase 6 does anyway. (b) `.waboros.checksum`, SHA-256 of the file with its own bytes zeroed:
   catches a flipped bit in `.text`; costs a hash of every file at every load (well under 1 ms for
   CoreMark's 50 KB with `hashlib`; not measured) and a SHA-256 in every foreign loader. (c) A
   CRC32 (IEEE, as `binascii.crc32` in Python's standard library) of the whole file computed with
   the CRC field itself zeroed, as a u32 in `.waboros.header` (taking the reserved word at 76):
   catches accidental corruption (a flipped bit in `.text`, a torn write) as well as (b) does,
   costs about 20 lines in a C loader and a negligible time per load; like (b), no defence against
   forgery. **Decided (maintainer, 2026-11-17): (a), no checksum.** (b) and (c) are rejected: as
   in Wasmtime's `.cwasm` and CPython's `.pyc`, cache files carry no checksum, and an atomic
   write-and-rename covers truncation.
6. **`pyc` and `mpy` artifacts.** (a) Not in format v1; when phase 6 serializes them, the same
   container with `e_machine` `EM_NONE`, the compiler target `pyc-cp313` (or `mpy-6`), the marshal
   data or `.mpy` in a `.waboros.pyc` section, and the same header, fat fallback and reader;
   JIT loaders refuse `EM_NONE`. (b) Outside ELF (a marshal file with a small header): no `EM_NONE`
   oddity, but a second container, a second fat rule and a second reader. Recommendation: **(a)**,
   specified when `pyc` serialization is a step.
   **Decided (maintainer, 2026-11-17): (a).**
7. **The file extension.** (a) `.wbo` ("waboros object"; ELF `ET_REL` files are objects); (b)
   `.wab` (taken by the Windows Address Book); (c) keep the working `.w2a` (the old project name).
   Not `.cwasm`: it is Wasmtime's and its files are not ours. Recommendation: **(a)**.
   **Decided (maintainer, 2026-11-17): (a) `.wbo`.**
8. **The feature-flag set (D66).** (a) Two u64 in the header: `features_required` (unknown bit:
   refuse) and `features_optional` (unknown bit: ignore), as ext4's incompat and compat sets; (b)
   one u64 plus a table in this spec of which bits are required: an old loader cannot know whether
   a new bit is required; (c) `e_flags`: tools interpret it per machine. Recommendation: **(a)**,
   both empty in v1. **Decided (maintainer, 2026-11-17): (a), two u64, both empty in v1.**
9. **Constant expressions.** (a) The fixed-width subset above; (b) the wasm bytes as they are
   (LEB128 and prefix decoding in every loader); (c) precomputed values where possible plus (a) for
   the rest: two encodings for one thing, and most cost nothing to evaluate anyway.
   Recommendation: **(a)**. **Decided (maintainer, 2026-11-17): (a), the fixed-width subset.**
10. **The type key.** (a) The byte encoding above, stored in the artifact; (b) keep `repr` and only
    store the keys: a loader could compare them, but the micro-compilers (D60, C) could not compute
    them, and the definition would be "whatever Python prints". Cost of (a): step 1 (about 80
    lines), no code byte changes. Recommendation: **(a)**. **Decided (maintainer, 2026-11-17):
    (a), the byte encoding.**
11. **Code-changing test switches when writing.** (a) The writer refuses while `WABOROS_JIT_NOPIN`,
    `NOLIVE`, `PAD` or `LOOP_ALIGN` is set (ROADMAP "Next" (6) asked to revisit them with the
    format); (b) record them in `config_sha256` and write anyway: test bytes then circulate as
    files. Recommendation: **(a)**; the switches stay for in-process studies. **Decided
    (maintainer, 2026-11-17): (a), the writer refuses.**
12. **The bounds mode of a portable file.** The writer records the mode it was given. Which mode a
    portable file gets by default is phase 6's (`waboros compile`): (a) `explicit` (runs on every
    row; guard on request, as an ISA level is, D33); (b) the compiling host's `auto` (the file
    then depends on the host that wrote it, against D35's cross-compilation); (c) `guard` (9 %
    faster on CoreMark x86-64, S0006, but refused, or regenerated if fat, on rows without a
    handler). Recommendation: **(a)**. **Decided (maintainer, 2026-11-17): (a) `explicit` by
    default, made final with the phase-6 API.**

## Docs this changes
When approved and implemented (step 7 unless noted):
* [ARTIFACT.md](../ARTIFACT.md) part 2: the header table, the section table, `ENTRIES` (now
  `.waboros.trampolines` and the store's one-copy rule), `CHECKSUM` (dropped, Q5),
  `RELOCS`, the `STACKMAPS` paragraph, "Other targets" and the file names replaced by a summary that
  links here; part 1 unchanged; part 3: the libcall block of 10 words (step 0, D58) and the "Per
  architecture" row "`CODE` alignment" reads `.text`; part 3's `tail_switch` and bridge wording ("one per process and
  convention, in the host's code area") becomes the store's one-copy rule.
* [DECISIONS.md](../DECISIONS.md): one entry recording the answers to D59's list and Q1-Q12 as
  decided by the maintainer (trampolines in every artifact with one copy per store, the type key,
  `cexpr`, the flag sets, no checksum); D29's
  artifact clause and S0006 step 6 closed by it.
* [S0006](S0006-guard-pages.md): Changes line (step 6 done here); its "Artifact" paragraph:
  `.waboros.traps` holds the fault map only, the frames table becomes `.waboros.addrmap`.
* [S0003](S0003-jit-context-abi.md): Changes line for the libcall reservation (step 0).
* DECISIONS.md D64 item 5 (one set of trampolines per compiler): add that a foreign host takes the
  store's copy from the first module artifact loaded (Q2).
* [S0013](S0013-gc.md): Changes line: `STACKMAPS` is `.waboros.stackmaps`; type keys are bytes.
* [ARTIFACT-CONTAINER.md](../ARTIFACT-CONTAINER.md): the decided line links here.
* TESTING.md (the artifact tests and the CI readers), ROADMAP.md ("Next" (6)), BENCHMARKS.md (file
  sizes, write, read and instantiation times), CLAUDE.md "Commands" (the switch and the `jit_dump`
  mode), `poc/artifact/elf64.py`'s docstring (the prototype, superseded by the package's writer).
* LESSONS.md: none expected; any trap found on the way.

## Progress
* Approved by the maintainer 2026-11-17.
* Step 0 done (2026-11-18): the libcall block is 10 words on both JITs (`context.LIBCALL_WORDS`;
  words 6-9 zero); ARTIFACT.md part 3 and S0003 updated. The spec suite's output is identical
  before and after on `jit-x86_64` and `jit-aarch64` (QEMU), both bounds modes; the D35 lists of
  an x86-64 and an aarch64 (QEMU) host are identical; code bytes changed in 217 of 2 527 compiled
  modules, each with words after the block.

## Changes
* 2026-11-17: created (draft).
* 2026-11-17: the maintainer's answers: Q1, Q3, Q4, Q6-Q12 decided as recommended; Q2 decided (b),
  trampolines in every module artifact (`.waboros.trampolines`, blobs at the end of `.text`, the
  store's one-copy rule checked byte for byte; the `kind` field and the trampoline artifact
  removed); Q5 decided (a), no checksum, with a CRC32 in the header considered and rejected.
  Every open question is decided.
* 2026-11-17: approved by the maintainer.
* 2026-11-18: step 0 implemented (D58's libcall reservation).
