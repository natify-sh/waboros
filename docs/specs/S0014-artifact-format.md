---
title: JIT artifact format v1 in the ELF64 container
kind: spec
id: S0014
status: implemented
superseded_by:
related: [D18, D23, D24, D29, D33, D35, D36, D44, D48, D54, D58, D59, D60, D61, D62, D63, D64, D65, D66, D70, D73, R2, R3, R6, S0003, S0006, S0013, S0015]
owner: maintainer
updated: 2026-11-24
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
| 4 | `EI_CLASS` | 2 (`ELFCLASS64`); the only class (D48: no 32-bit JIT). *(2026-11-24, maintainer: it stays the only class for every target, 32-bit ones included, should they come: the file is a data format, a 32-bit target's code is position-independent and is embedded as a blob, not linked by the system linker; where one `e_machine` covers both widths (RISC-V, MIPS) the `isa_level` numbers of the 32-bit ISA are disjoint from the 64-bit ones.)* |
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
| next | `.symtab` | `SHT_SYMTAB` (2) | 0 | 8 | always; `sh_entsize` 24, `sh_link` = `.strtab`, `sh_info` = the symbol count (no global symbol; S0015) |
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
`st_value` its code offset (the function's C entry, S0015), `st_size` up to the next function's
start, the first host thunk, the module's shared C entry part or `data_start`; one `STT_FUNC` per
host thunk, named `thunk[<i>]` (`<i>` the imported function's index); `__waboros_centry_<conv>`,
the module's shared C entry part (S0015: it follows the last body and runs to `data_start`;
`<conv>` the first convention of `.waboros.trampolines`, as the C entries are one convention on
every OS); per convention, in the order of `.waboros.trampolines`, `STT_FUNC` symbols
`__waboros_bridge_<conv>` and `__waboros_tail_switch_<conv>` (`<conv>` `sysv`, `win64`, `aapcs64`;
`st_size` reaches the next symbol's start or the blob's end); on aarch64 and riscv64 the mapping
symbols of AAELF64 and the RISC-V psABI, `$x` (`STT_NOTYPE`) at 0, `$d` at `data_start` when the
data area is not empty, and `$x` at the first trampoline, so `objdump -d` does not decode jump
tables. **There is no global symbol**: S0015 removed `__waboros_entry_<conv>` with the entry (D59,
D62), so `sh_info` is the symbol count. A writer finds `centry` from the jump of the first C-entry
stub; no field of the file carries it. Names from the wasm name
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
| 104 | `str` | `compiler`: `waboros <version> <target> <target version>`, four tokens separated by one space, ASCII, e.g. `waboros 1.2.3 jit-x86_64 0` or, in a development build, `waboros 0.0.1.dev207-b9515381851a1c6b jit-x86_64 0`; `<version>` is the package version of a release, verbatim; a development build (a local part `+...`, a `.devN`, or the fallback `0.0.0`) writes the version without its local part, `-` and the first 16 hex digits of a SHA-256 over its compiler sources (Changes, 2026-11-20), `<target>` the compiler target that D64 keys the part-3 "Per architecture" table by (*amended by D70: only the compiler's rows of that table; the boundary row is per architecture*), `<target version>` the target's `version`; no token is empty and `<target version>` is at most 9 decimal digits |

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
the backend's `finish` returns, then zero bytes to the next multiple of 16 (part of the data area; written as zero, not checked
by a reader, which cannot tell them from data-area bytes).
Then the **compiler blob** of each convention (`.waboros.trampolines`: the bridge, then `tail_switch`
with its `restore` point; no entry and no `sp_stub`, S0015), each at the next offset that
is a multiple of 16, the gaps zero bytes; each blob is exactly what the architecture's
`conventions()` gives for that convention. Position-independent: branches are
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

A supertype is an earlier type, and a supertype chain is at most 63 supertypes deep (the
validator's bound and Wasmtime's); a deeper one is refused, so the walks over chains (`heap.rtt`,
`instance.type_id`, `match`) are bounded by the format.

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
are not supported). The limits obey the validator's rules: `min` is not above `max` when present;
a memory's `min` and `max` are at most 65536 pages (i32) or 2^48 (64-bit addresses); a table's at
most 2^32-1 (i32) or 2^64-1 elements. This applies to imports, tables and memories alike.

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

**Typing rules the reader applies** (the validator's, for this subset; a violation is a refusal):
* The operand types of each operation fit its signature, bottom first: `i32.add`, `sub`, `mul` two
  i32, the i64 ones two i64, `ref.i31` an i32, `any.convert_extern` an extern-hierarchy reference and
  `extern.convert_any` an any-hierarchy one (never a function reference), `struct.new` one value
  per field fitting the field's unpacked type (i32 for an i8 or i16 field), `array.new` a value fitting the element and an
  i32 length, `array.new_default` an i32 length, `array.new_fixed` n values fitting the element;
  the stack never underflows and holds exactly one value at the end. `ref.null` fits any nullable
  reference type (no heap type is written).
* The result fits its use: a global's or a table's value type (by subtyping for references), an
  element item's element type, an active segment's offset the address type of its table or memory.
* `global.get` names an immutable global only. A global's initializer reads imported globals and
  the globals before it; a table's `init` reads imported globals only. `ref.func` names a function
  that has a record (`.waboros.context`).
* `struct.new` and `struct.new_default` name a struct type, the array operations an array type;
  an allocation needs `.waboros.context` `heap` 1.
* An element segment with `items` 1 whose every item is a `ref.func` is refused: the writer uses
  `items` 0 for it.

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
u32 nsigs,     nsigs x u32 type index          one signature word each (strictly increasing;
                                               a function type)
u32 nthunks,   nthunks x u32 code offset       the host thunk of each function import, in import
                                               order (nthunks = the number of function imports)
u32 nrtts,     nrtts x u32 type index          one RTT word each (struct or array types,
                                               strictly increasing)
u32 nthrown,   nthrown x u32 tag index         one RTT word each, after those (strictly increasing)
```

`heap` 0 requires `subtype`, `nrtts` and `nthrown` 0, and no constant expression of the module
allocates (`struct.new`, `struct.new_default`, `array.new`, `array.new_default`, `array.new_fixed`). The context's words then follow from
part 3 in its order: memories = imported + defined; the heap's two words when `heap`; tables =
imported + defined; globals = imported + defined, two words for each **defined** `v128` global;
function imports; the libcall block; `nsigs` signature words; `nrecords` records of 3 words; tags =
imported + defined; when `heap`, the 8 GC service words, `nrtts + nthrown` RTT words and the
3-word IS_SUBTYPE record; at least 18 words in total (before S0015 the entry read memory 0's words; the boundary no longer does: this compiler's C entry reads them, and a module with no memory has them zero).
When `heap`, the loader also writes a function-type record for every function type of the module
that has a supertype or is not final (part 3, "Type-info records").

### `.waboros.stackmaps`
The `STACKMAPS` payload of ARTIFACT.md part 2, unchanged (S0013, D65): `u32 nsites`, the 16-byte
site records sorted by `ret`, `u32 nmaps`, the pool of `refs` and `live` bitmaps. Required; a
loader decodes it with the checks listed there and refuses one that does not parse exactly, or whose
sites are not strictly increasing by `ret` (a duplicate or an unsorted one).

### `.waboros.traps`

```
u32 n, n x (u32 fault_offset, u32 resume_offset)   fault_offset strictly increasing
```

The guard fault map of S0006 ("Artifact", the handler contract): `n` is 0 unless `BOUNDS_GUARD`
is set, every offset (fault and resume) is below `data_start`. This section holds the fault map only. The address
map for `Trap.frames` (code offset to wasm function and byte offset), which part 2 reserved in
`TRAPMAP` after the fault map, is not produced by the compiler; when it is, it is a separate
optional section `.waboros.addrmap` (Wasmtime's `.wasmtime.addrmap`), which older loaders skip, so
it needs no version and no flag. Appending it to `.waboros.traps` would break the exact parse.

### `.waboros.wasm`
The exact wasm bytes whose SHA-256 is `module_sha256`. Its presence makes a **fat** file (D18).

### `.waboros.trampolines` (Q2)
*Amended by [S0015](S0015-boundary-convention.md) (step 4, 2026-11-23):* the section carries the
**compiler's blob only**, one per host convention. The entry and the stack-pointer stub are the host's
own by contract (part 3, "Entry"); the waboros runtime builds them in `entry.py` (`target.runtime()`).
Before S0015 the section held the entry, the bridge, the stack-pointer stub and `tail_switch` in a
40-byte record; a file of that layout is refused by `read` on the payload's size.

The bridge and `tail_switch` of part 3, for every host convention of the architecture, are in every
module artifact (part 2's `ENTRIES`, D59; D23: one artifact for every OS, so x86-64 carries both
conventions):

```
u32 n
n x blob:  u8 id (1 sysv, 2 win64, 3 aapcs64; append-only, valid per e_machine), u8 0, u16 0,
           u32 start, u32 size,                        the blob in .text
           u32 bridge,                                 offset in .text
           u32 tail_switch, u32 restore,               offsets in .text
           u16 restore_ret_at, u16 restore_up          the restore frame (S0015)
```

28 bytes per blob. x86-64 writes `sysv` then `win64`; aarch64 writes `aapcs64` (one convention on
Linux, macOS and Windows); riscv64 defines its own with its baseline. `restore_ret_at` and `restore_up`
are the bytes from the stack pointer at `tail_switch`'s call to the real return address and to the
caller's stack pointer (x86-64 56 and 64, aarch64 8 and 80). Every offset lies inside its blob, with
no exception: a compiler always ships a `tail_switch`, and the reader refuses `0xFFFFFFFF` (the "none"
value, removed from v1 by S0016) by name; `start` of the first blob is the end of the data area. The reader checks the payload's
size, the ids, the reserved bytes, the placement and the ranges, then compares. A loader finds the
blobs only through this payload, never through a symbol (D62). Sizes and the file growth are in
BENCHMARKS.md, "Artifact size".

**One copy per store.** *(Amended by S0015, D70, 2026-11-23: the rule now covers the compiler's
blob; the entry is the runtime's and not in the file, the walk knows the set of entries and the
restore points of each compiler present, and the copy is per compiler string, as the last
sentence says.)* The frame walk recognises the entry's return point and `tail_switch`'s
`restore` by address, and `tail_switch` checks for its own `restore`, so every instance of a store
runs on one copy of the trampolines of the host's convention (part 3; D64 item 5 allows one copy per
compiler, shared by stores). That copy is the store's (a foreign host: the first module artifact
loaded into the store provides it; the waboros runtime: the copy it assembles in the process, below).
Every later artifact's blob for that convention, and its `bridge`, `tail_switch`, `restore`
relative to the blob's `start` and the two restore numbers, **must be byte-identical** to the
store's copy. The loader checks this (a compare of 202 or 203 bytes on x86-64, SysV or Win64, and 236 on aarch64, measured), and does not rely on the `compiler` check alone: equal
`compiler` strings are the writer's promise, the compare is the proof, and its failure means a
corrupt or forged file. A mismatch is a refusal (thin) or a regeneration (fat). A copy is never
shared between compiler strings, even when the bytes are identical: equal `HOST_BRIDGE`
addresses must mean the same compiler string and the same internal convention (S0015, D71:
`jit-x86_64` calls a record whose context has its own `HOST_BRIDGE` at its body). Two rules, by
who loads:

* **A foreign host** (no compiler, D36): the store's copy is the first artifact's; every later
  artifact's record and blob for the running convention are compared with it as above, and the
  other conventions are checked for being well-formed only (every offset inside its blob), not
  compared: such a host cannot make them. It supplies its own `entry` and `sp_stub` (S0015).
* **The waboros runtime** keeps assembling its trampolines in the process (`target.trampolines()`,
  one per process and architecture: its own `entry` and `sp_stub`, `target.runtime()`, with the
  compiler's blob of its convention) and runs every store on them; it never executes an
  artifact's copy. As it can assemble every convention on any host, and requires its own exact
  `compiler` string anyway (Q3), `read` compares **every** convention's record (`size`, `bridge`,
  `tail_switch`, `restore` relative to `start`, the two numbers) and blob with what that
  architecture's `conventions()` gives: a mismatch in any is a corrupt or forged file, refused
  thin and regenerated fat; the in-process and the file copies cannot drift; and every file
  `read` accepts is the writer's, `write(read(b)) == b`. The writer writes exactly
  `conventions()`'s bytes for every convention.

The collector module (D61) is an ordinary module artifact compiled from
`runtime/gc/collector.wasm`.

### What instantiation reads: inventory
Every field `JitInstance.__init__`, `Instance`, `JitFunc`, `link.py`, `heap.py`, `gc.py` and
`collect.py` read from `Compiled` and from `Compiled.module` (before step 3 the `ValidModule`;
since step 3 the view of the keys below, `artifact.module_view`), and where the artifact carries it. The reader returns the same `Compiled` with a module view that has exactly
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
| `Compiled.subtype` (`gc.subtype_sites` over the bodies, at compile time since step 3; before it, at instantiation) | instance | `.waboros.context` `subtype` |
| `Compiled.code`, `data_start` | `Instance`, tools | `.text`, `.waboros.funcs` |
| `Compiled.funcs` (code offset, result slots) | `Instance.call`, `JitFunc.record` | `.waboros.funcs` (slots from the type) |
| `Compiled.layout` | `Instance` | derived ("`.waboros.context`") |
| `records`, `sigs`, `thunks`, `rtts`, `thrown` | instance | `.waboros.context` |
| `flags`, `faults` | `target.instantiate`, instance | header, `.waboros.traps` |
| `stackmaps` | instance, walk, collector | `.waboros.stackmaps` |
| `Trampolines` (`target.trampolines()`): the runtime's part (entry, sp_stub, entry_ret, saved: its own, `target.runtime()`, not read from the file) and the compiler's (bridge, tail_switch, restore, restore_ret_at, restore_up) | `Instance`, the walk, `tail_switch` | the compiler's part: `.text` + `.waboros.trampolines` (one copy per store, checked); the runtime's: not in the file |
| `refmasks`, `v128masks`, `insns`, `codes`, `validated`, `gc`, `anyext` | the compiler only | not carried |

## Semantics: the loader

**Trust.** An artifact is native code: loading one from an untrusted source runs untrusted code,
as Wasmtime's `Module::deserialize` is `unsafe`. The reader's checks are against truncation,
corruption, other versions and forged *formats*: a malformed file is refused with a message and
never crashes the interpreter; they are not a sandbox.

A context-ABI amendment (words appended in reserved space) is announced by a `features_required`
bit (D66), so a loader that does not know it refuses the file instead of running it.

**Known limitations of v1** (the step-8 review; what the reader does not check):
* Metadata is not cross-checked against the code: code and thunk offsets, the `heap` and `subtype`
  decisions, the per-site geometry of a stack map (`shift`, `ret_at`, `up`, `ret`). A corrupt but
  well-formed stack map misleads the stack walk of a collection; this is the trust model above,
  not a defect to close by checks (the file is native code anyway).
* Declared subtyping is not validated: whether a type's `final` flag is respected by its subtypes
  and whether a subtype's composite type matches its supertype's (the keys are checked as
  canonical, not the relation).
* Export names are not checked for uniqueness, and the start function's type is not checked to be
  `[] -> []`.
* This loader runs `isa_level` 1 only (a higher level is refused, part 1).

**Order of checks** (each failure names its reason; "refuse" means a thin file is refused and a
fat file is regenerated, below):
1. Identification: `EI_MAG`, `EI_CLASS` 2, `EI_DATA` 1, `EI_VERSION` 1, `EI_OSABI` 0xCB. Otherwise
   "not a waboros artifact": no fallback, the file is not ours.
2. `EI_ABIVERSION` is a `format_version` the loader reads (v1 loaders: 1). Otherwise refuse.
3. The ELF header fields of the table above, the section header table inside the file, every
   section inside the file, names unique, every required section present, no `SHT_REL` or
   `SHT_RELA` section (v1 has none; one would mean code the loader cannot place). Sections with
   other names are skipped (D62; `.note.GNU-stack` or a later descriptor section need no version).
   `sh_flags` of `.waboros.*` sections are ignored; sections are found by name (D62). The layout
   is the writer's: an unknown section is not before `.text` and not after `.shstrtab`, known ones
   keep their order; `.shstrtab` is exactly the section names in section order, each
   NUL-terminated, without suffix sharing; every section's offset is the running position rounded
   up to its alignment (a power of two), the gaps are zero bytes, and the section header table
   follows the last section at a multiple of 8 and ends the file; each header's other fields are
   the format's (type, address 0, link, info, entry size).
4. `.waboros.header` parses exactly.
5. Compatibility: `e_machine` is the running architecture (a cross-compiled artifact is never run on
   the compiling host, D35); `ctx_abi_version` is 1; the `compiler` string is the store's
   (*D70: until phase 6; from phase 6 the architecture, `format_version` and `ctx_abi_version`
   decide, S0015 "Compatibility", and the trampoline compare of step 6 moves from `read` to store
   attach*; D64's "the store's compiler" is gone) and its versions are acceptable (Q3); `isa_level` and every `isa_features` bit are known
   and present on the CPU (part 1; the message names the missing features); every `flags` bit is
   known; `BOUNDS_GUARD` only where this host can run guard code (S0006: a handler for its row,
   installed and ours, and a guard at least `2^GUARD_LOG2`); every `features_required` bit is known.
6. Every payload parses exactly with its checks; the trampolines equal the store's copy (the
   waboros runtime: every convention's compiler blob equals `conventions()`'s; "`.waboros.trampolines`").
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
*(amended by S0016: with the target passed to `load()`; a store may hold several compilers)*
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
  `conventions()`'s for that convention (both x86-64 conventions on any host; S0015: the compiler's
  blob only); an instance loaded from a file runs on the process's copy, and a file whose blob of
  any convention differs by one byte (or whose `bridge`, `restore` or restore numbers are moved) is refused thin and regenerated fat; in a C-style test of the
  store rule, two artifacts loaded into one store share the first one's copy and a cross-instance
  tail call and a walk across both work.
  S0015 step 4's tests are `tests/artifact/test_trampolines.py` (the 28-byte layout, no runtime
  trampoline and no global symbol in the file, one copy per store) and `tests/artifact/test_old.py`
  (the pre-S0015 fixtures refused, the new `v1-s15-*` files run); the compare at store attach
  waits for phase 6.
* **The spec suite through files**: `WABOROS_JIT_ARTIFACT=1 uv run python -m tests.spec.run
  --engine jit-x86_64` (every compiled module written, read back and instantiated from the read
  form, with no `ValidModule` behind it), both bounds modes, with `WABOROS_JIT_WALK_CHECK=1` and
  the GC switches for `gc/` and `exceptions/`; then `uv run python tools/spec_diff.py jit-x86_64
  <the same with the switch>` gives no difference. The same for `jit-aarch64` under QEMU and on
  the native arm64 runner. *(As built, step 7, 2026-11-23: run by hand under QEMU on both JITs, both
  modes (Progress, step 3); from 2026-11-23 (maintainer) the ci `spec` job runs `jit-x86_64` and
  the `arm64` job runs `jit-aarch64` natively with `WABOROS_JIT_ARTIFACT=1`, guard mode, with the
  expected-failures file of each, which the run must meet.)*
* **CoreMark through a file** on both JITs (`tools/run_coremark_jit.py` with the switch): the self
  check passes; run time alternated with the direct path (no change expected: the code is the
  same bytes); write time, read time, instantiation time and file size recorded in BENCHMARKS.md.
* **Determinism (D35)**: each module written twice in one process and in two processes gives the
  same bytes; in CI's arm64 job, the files written there for `jit-aarch64` equal the files written
  on x86-64 (as built: `tools/artifact_dump.py` writes the files and lists their sha256, and the
  `artifacts` / `artifacts-diff` jobs of `platforms.yml` compare six hosts, step 6; `tools/jit_dump.py
  --files` writes one file per module) and the x86-64-written file runs there (as built:
  `test_old.py::test_view_runs` reads and runs aarch64 files written on x86-64, `v1-s15-small-aarch64.wbo`
  (since S0017 P1 `v1-d75-*-aarch64-*`), in the native aarch64 jobs of `platforms.yml`: Linux, macOS
  and Windows on Arm).
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
| 2.1 | The reader strict at the container and constant-expression level: layout exactly the writer's (`.waboros.*` flags and symbol contents not read, D62), value types of `cexpr` against their use; pinned section headers, symbols, `config_sha256` | implementer | the writer's files write back to the same bytes, every other accepted file to its canonical form (byte-flip loop, 0 bad); the round trip on the spec suite unchanged | src ~100, tests ~100 |
| 3 | Instantiate from the read form: the instance takes the module view and `subtype` from the artifact; the `WABOROS_JIT_ARTIFACT=1` switch | jit-engineer | the spec suite through files at parity, both JITs, both modes, walk check and GC switches; CoreMark through a file; timings recorded | src ~120, tests ~80 |
| 4.1 | Loader rules: `artifact.load` (the checks in order, fat regeneration), the compiler string by option (c), `WABOROS_JIT_ARTIFACT=1` through `load`, the refusal tests | implementer | `tests/artifact/test_refuse.py`; the spec suite through files at parity; D35 lists unchanged | src ~150, tests ~300 |
| 4.2 | The GC heap's lazy commit (D69): the header and type area, the tid map (whole, with the first function-type record) and the semispaces committed apart; layout, `collector.wat` and code unchanged | jit-engineer | `pytest -q`; `gc` and `exceptions` with the GC switches and the walk check in both bounds modes, both JITs; the spec suite at parity; D35 lists unchanged; committed bytes before/after in BENCHMARKS.md | src ~50, tests ~100 |
| 4.3 | The committed v1 files and their test, after step 5 (which adds a required section) | implementer | the old-artifact test; `pytest -q` | tests ~60 |
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
* **The trampolines are the runtime's, and no artifact carries them** (Q2 (c), seen from the
  runtime's side; considered again and rejected 2026-11-21; *partly reversed by S0015, D70,
  2026-11-23: `entry` and `sp_stub` are the runtime's and leave the file, the bridge and
  `tail_switch` stay the compiler's; "D64 has one compiler per store" below no longer holds*): the trampolines belong to the
  compiler, not the runtime. D64 has one compiler per store and part 3's register table is per
  compiler (a sljit micro-compiler keeps the context in another register), and a runtime may run
  code from compilers it does not know (D60: micro-compilers of other architectures shipped as
  `.wasm`; cross-compilation), so whoever produces the code must produce its trampolines. The
  symbol collision of static linking is answered by per-artifact name prefixes, which D62
  already reserves.
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
   loader checks every later artifact's copy byte for byte against it. (c) was considered again
   on 2026-11-21 as "the trampolines are the runtime's" and rejected: they are the compiler's
   (D64, D60; "Alternatives considered"). *Partly reversed for `entry` by S0015 (D70): `entry` and
   `sp_stub` are the runtime's; the bridge and `tail_switch` remain in every artifact.*
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
* Step 1 done (2026-11-18): `frontend.types.key` returns the 32 raw bytes of "Canonical type key"
  (no `repr` left in `src`; `heap.records` keys `b"exn " + key`; generated `pyc` code embeds the
  key as a bytes literal). `tests/test_type_key.py`: the encoding by hand (final, supertype,
  in-group and out-of-group references, `exn`/`noexn` and other abstract codes, shorthand equal to
  long form) and, over the 2 526 validating modules of the pinned suite (3 949 types), the old
  `repr` key as the oracle: new keys equal exactly when old keys equal (no collision on the suite's
  types; injectivity rests on the encoding being prefix-free, which the hand cases pin byte for
  byte: the step-1 review's mutations of every count and tag fail them). `linking`, `type-*`,
  `gc/` and `exceptions` (1 129 pass, 3 skip) are the same on `pyc`, `jit-x86_64` and
  `jit-aarch64` (QEMU); the D35 lists (5 979 modules, 2 527 compiled) are byte-identical before
  and after on both JITs.
* Step 2 done (2026-11-18): `targets/jit/common/artifact.py` (`write`, `read`, `ModuleView`,
  `ArtifactError`; standard library only) and `tests/artifact/`. Round trip on the 2 526 modules of
  the pinned suite that the JITs compile, on `jit-x86_64` and `jit-aarch64` (cross-compiled),
  explicit and guard: each equal in the inventory's fields, `write(read(b)) == b`, thin and fat,
  written twice identical; CoreMark: thin 55 048 / fat 74 592 bytes (x86-64, explicit), 51 408 /
  70 952 (guard), aarch64 49 456 / 69 000 and 46 720 / 66 264 ([BENCHMARKS](../BENCHMARKS.md),
  "Artifact size"). Not yet: `.waboros.trampolines`, the trampoline symbols (step 5), the loader's
  compatibility checks (step 4), instantiation from a view (step 3).
* Step 2.1 done (2026-11-18): `read` checks the container's layout against the one the writer
  produces (offsets, gaps, header fields, order, section names) and the value type of every
  constant expression against its use. `tests/artifact/test_strict.py` pins the section header
  table, the symbols and `config_sha256`, one refusal per rule, what the reader ignores, and a
  byte-flip loop (3 000 flips each on CoreMark, a GC module and a small module, `pytest -k
  byte_flips -s` prints the classes: accepted flips are `exact`, `ignored`, `unwritable` (a header
  `flags` field that `write` refuses) or `unknown` (renamed section); `bad` is 0).
* Step 3 done (2026-11-19): `JitInstance` takes a `Compiled` or a `ModuleView` (the same field
  names) and reads only the module keys of the inventory (`artifact.INVENTORY`): `Compiled.module`
  is now `artifact.module_view(mod)`, the ValidModule cut to those keys, so the direct path and
  the file path are one path and a read of a body (`codes`, `insns`) fails on both. `subtype` is
  `Compiled.subtype`, decided at compile time (`gc.subtype_sites`), or the artifact's bit; the
  instance no longer scans bodies. Constant expressions are evaluated with an explicit stack (one
  evaluator for both trees). `WABOROS_JIT_ARTIFACT=1` (TESTING.md, "Through files") writes,
  reads and instantiates every module: the spec suite gives 64 229 pass, 0 fail on `jit-x86_64`
  and `jit-aarch64` (QEMU), both bounds modes, `tools/spec_diff.py` both ways empty against the
  direct path; `gc exceptions` under the walk check and the three GC switches 905 pass, 0 fail
  (both JITs, both modes). The D35 lists are unchanged on both JITs, both modes; CoreMark through a
  file runs at the direct path's speed ([BENCHMARKS](../BENCHMARKS.md), "Instantiation from an
  artifact"). Code bytes and the format are unchanged.

* Step 4.1 done (2026-11-20): `artifact.load(b, *, target)` (identification, `read`, compatibility
  in the order of step 5, then fat regeneration) and `artifact.compiler_string(target)`, which
  `write` and `load` share; `JitTarget.instantiate` under `WABOROS_JIT_ARTIFACT=1` goes through
  `load`. `tests/artifact/test_refuse.py`: every refusal of the Test plan's list that is about the
  header, thin (refused, message names the field and both values) and fat (regenerated, an export
  called). Spec suite through files 64 229 pass, 0 fail, both bounds modes; without the switch
  unchanged; D35 lists unchanged on both JITs. Left: 4.2 (GC heap lazy commit) and 4.3 (committed
  v1 files, after step 5); the trampoline rule of step 6 is step 5's.
* Step 4.3 done (2026-11-21): the v1 files are committed under `tests/artifact/data/` (one small
  module, `v1-small.wat`/`.wasm`, as fat and thin files for `jit-x86_64` and `jit-aarch64`, explicit
  bounds mode, compiler string `waboros 0.0.1.dev207-dc5da8fdb1c107b3 jit-<arch> 0`, made at
  commit `9340759` by `tools/make_v1_artifacts.py`, never regenerated). `tests/artifact/test_old.py`:
  `read` and its inventory on all four; the read view instantiated directly on the native
  architecture (context ABI v1 runs old code); `load` of the thin file (this process's compiler
  string: loads; another: refused with the "compiler" message); `load` of the fat file runs; a copy
  with `EI_ABIVERSION` 2 refused thin and regenerated fat. Another architecture's files get the
  read, the refusal (`e_machine`) and the version tests. Everything of the plan is done.
* 2026-11-21: **steps 6-8 and the freeze of v1 wait for [S0015](S0015-boundary-convention.md)** (D70,
  the boundary convention; draft). It changes what a record's `code` holds (a C entry), what
  `.waboros.funcs` names, what `.waboros.trampolines` carries (the compiler-private `bridge` and
  `tail_switch` only; `entry` becomes a contract the host fulfils) and the D64 sentences of this
  spec and of ARTIFACT.md part 3. No version bump. The committed files of step 4.3 stay as
  pre-S0015 fixtures (`test_old`: thin refused, fat regenerated), and new v1 files are added
  (maintainer, 2026-11-21). The exact `compiler` string stays the loader's rule and the
  trampoline compare stays in `read` until phase 6 (S0015, "Compatibility").

* Step 6, partial (file identity, 2026-11-23): `tools/artifact_dump.py` writes the 16 `.wbo` files of
  CoreMark and `v1-small.wasm` (thin and fat, both JITs, both bounds modes) and lists their sha256;
  `platforms.yml` jobs `artifacts` / `artifacts-diff` run it on Linux x86-64 and arm64, Windows x64
  and ARM64, macOS x86-64 and arm64 and fail on any difference, naming the file.
* Step 6, done (independent readers, 2026-11-23): `tools/elf_readers.py` (CI job `jit`, Linux x86-64
  only; it reads the aarch64 files too, the arm64 job adds nothing) checks the same 16 files (the
  round-trip test set is the pinned spec suite, too large to write for five readers; CoreMark and
  `v1-small` cover both JITs, both modes, thin and fat): `readelf -aW` and `llvm-readobj --all` print no
  warning; pyelftools gives our reader's sections (name, offset, size, payload) and function symbols
  (name, value, size); `llvm-objdump -d` has a label for every function symbol (`wasm[i]`, `thunk[i]`,
  `__waboros_*`); `eu-elflint --gnu-ld` exits 1 with exactly two complaints, the private identification
  and nothing else, which the check allows and any other line fails: `unsupported OS ABI e_ident[7] ==
  '<unknown>: 203'` and `unsupported ABI version e_ident[8] == 1`. `tools/isa_all.py` now writes
  an artifact per compiled module (`jit_dump.py --files`), reads the files back and checks the code up to
  the first trampoline blob (the data area excluded) and every trampoline blob (x86-64: 954 blobs
  against 952 before, aarch64: 931 against 930; identical blobs are checked once, so the
  trampolines add one per convention, not one per module).
* Step 7 done (2026-11-23, docs only): every document of "Docs this changes" checked against the code
  (`artifact.py`, the targets' `conventions()` and `runtime()`) and fixed where it was still the
  draft of part 2: ARTIFACT.md part 2 is a summary of this spec (ELF sections, no `ENTRIES`,
  `RELOCS` or `CHECKSUM`), part 3's `.text` alignment row and `tail_switch` wording; D73 records the
  decisions; S0006, S0013, ARCHITECTURE.md, ARTIFACT-CONTAINER.md, README.md, TESTING.md and ROADMAP.md
  follow. Open for step 8: the spec suite through files runs in no CI job (Test plan note).
  *(2026-11-23: now run, by the ci.yml `spec` and `arm64` jobs, with `WABOROS_JIT_ARTIFACT=1`.)*

* Step 8 done (2026-11-23): an independent review of the reader, the frozen part, the refusals,
  `cexpr` evaluation and determinism (fuzzing: every truncation of 12 files, byte flips,
  field-targeted and header mutations; every failure an `ArtifactError`, every accepted payload
  flip written back to the same bytes). Its findings are closed in the reader with a test each
  (`tests/artifact/test_review.py`; Changes, 2026-11-23); a re-review confirmed them closed. What
  the reader does not check is under "Known limitations of v1".
## Changes
* 2026-11-23: the **working freeze of v1** (maintainer, 2026-11-23): the format of S0014 and the context ABI with S0015's boundary are the project's working ABI, not a release. Nothing in them changes silently: a change goes through a spec or a decision and updates the documents and the tests, without a version bump. The numbers in the file stay `format_version` 1 and `ctx_abi_version` 1 (0 in `EI_ABIVERSION` is what any ELF carries and is refused), so v1 is the current version. **The final freeze of v1** comes before the merge to `main`, once the first sljit micro-compiler (D68) compiles code across the S0015 boundary (its C entries, the form-2a sibling tail calls and a record with no `tail_switch`, untested until then) and the C reference loader (D36) embeds it and reads and runs the committed files.
* 2026-11-24: Amended 2026-11-24, maintainer: the final freeze of v1 comes once the first real micro-compiler, our core in C over sljit (D68), compiled to wasm, produces files across the S0015 boundary and the C reference loader (D36) reads and runs them and the committed files; S0016's `jit-sljit` is a step towards it, not part of the condition; the "none" `tail_switch` value leaves v1 (S0016), so the condition no longer names form 2a or a record without `tail_switch`.
* 2026-11-24: Amended again 2026-11-24, maintainer: the final freeze of v1 comes once the micro-compiler from our C sources (D68) is built to wasm and natively, the C reference loader with the native compiler embedded (x86-64, aarch64) compiles and runs modules, and the same loader built without the compiler runs files cross-compiled by the wasm build and the committed files.
* 2026-11-17: created (draft).
* 2026-11-17: the maintainer's answers: Q1, Q3, Q4, Q6-Q12 decided as recommended; Q2 decided (b),
  trampolines in every module artifact (`.waboros.trampolines`, blobs at the end of `.text`, the
  store's one-copy rule checked byte for byte; the `kind` field and the trampoline artifact
  removed); Q5 decided (a), no checksum, with a CRC32 in the header considered and rejected.
  Every open question is decided.
* 2026-11-17: approved by the maintainer.
* 2026-11-18: step 0 implemented (D58's libcall reservation).
* 2026-11-18: step 1 implemented (the canonical type key in bytes).
* 2026-11-18: step 2 implemented (writer and reader). Points the code needed that the text did not
  settle: (1) `ref.null` has no immediate in the file, so a read expression is `("null", None)`
  where the front end has `("null", heap type)`; nothing in the instance reads it. (2) `Compiled`
  carries neither the compiler target nor the wasm hash, so `write` takes `target=` and
  `module_sha256=` (or `wasm=`) besides the bounds mode, which is `Compiled.flags`. (3) The isa
  name of aarch64 level 1 in `config_sha256` is `armv8.0-a`. (4) Until step 5 the section
  `.waboros.trampolines` is not written and the reader does not require it; no global symbols exist
  yet (`sh_info` is the symbol count). (5) was a range check of `global.get` against every global; fixed (see the next line).
* 2026-11-18: step 2 review fixes: an element segment's function indices (`items` 0) must have a
  record, as `ref.func` expressions do; `global.get` in global `k`'s initializer reads only
  imported globals and earlier ones (index < imported + k); the `compiler` field's target version
  is the target class's `version` (`write` has no `target_version` parameter any more).
* 2026-11-18: the `compiler` field's waboros version (step-2 review): `__version__` from
  setuptools-scm carries a local part (`+g<hash>.d<date>`) that changes with `uv sync` and a dirty
  tree, not with the code, so two hosts at one commit wrote different files and a code change
  without a re-sync kept the same string. **Decided (maintainer, 2026-11-18): (c)**: a release
  build writes its release version; a development build writes the version without its local part
  plus a SHA-256 of the compiler-side sources (`frontend/`, `jit/`), computed once per process, so
  the string is deterministic and changes with the code. Implemented in step 4 (the loader rules
  that compare the string), with a test that two copies of one tree write identical files.
* 2026-11-18: the maintainer moved the container strictness, the cexpr value types and the pinned
  tests from steps 3 and 4 to a new step 2.1, done before step 3. Decisions: the reader checks each
  section's header against the expected one (offset from the running position and the alignment,
  type, address, link, entry size, name offset) and rebuilds `.shstrtab` from the section names,
  rather than calling `write` and comparing: the messages name the section, and the check does
  not depend on the writer's compiler string (a file of another waboros version still reads).
  **What is not checked (D62)**: `sh_flags` of `.waboros.*` sections, `sh_info` of `.symtab`, and
  the contents of `.symtab` and `.strtab` (only their presence, types, entry size, `sh_link` and
  place): no loader reads them, a later amendment may set flags, add symbols or prefix names.
  **Unknown sections are skipped**, and so is `.waboros.trampolines` until step 5. `.text` is
  section 2 (the spec's table), so an unknown section may sit anywhere after `.text` and before
  `.shstrtab` (`e_shstrndx` is the last section); known ones keep their order. Its header must
  have the canonical name offset, address 0, a power-of-two alignment (0 and 1: none) that its
  offset respects, an offset at or after the previous section's end with zero gap bytes, and its
  bytes inside the file before the section headers (no overlap with other sections, the ELF
  header or the section header table). Whatever its type, it takes file space (`SHT_NOBITS` with a
  size above 0 is refused as one that runs past its place, as step 3 of "Semantics: the loader"
  requires); type, flags, link, info and bytes are not interpreted and are not kept in the view.
  `.symtab`, `.strtab` and `.shstrtab` are required. **The round-trip property**: (i) for every
  file this writer writes, `write(read(b)) == b`; (ii) the reader accepts a superset: files that
  differ only in `.waboros.*` flags, `.symtab` `sh_info`, symbol or string contents, the header
  fields `write` regenerates (`config_sha256` to `compiler`, until step 4 checks them) or
  unknown sections; for those, `write(read(b))` is the canonical form, which reads back to an
  equal view, except a header `flags` value `write` refuses (`ValueError`) until step 4 checks it on
  read (the test compares views without the regenerated header fields). Constant
  expressions read only immutable globals (`global.get` of a mutable one is refused), and a
  table's initial value reads imported globals only (as the validator, Wasm). A table of a
  non-nullable type needs an initial value. `ref.null` has no heap type in the file, so its value
  type fits any nullable reference. An element segment of items 1 whose items are all `ref.func`
  (or none) is refused, as the writer writes items 0 for it; an active element segment's type
  must fit its table's.
* 2026-11-18: still to do. Step 3: constant expressions are evaluated without recursion (a
  300 000-deep `i32.add` chain reads fine and would overflow `_const`). Step 4: the header's
  `flags`, `isa_level`, `ctx_abi_version` checks, which the round trip does not yet cover
  (`config_sha256`, `isa_level`, the compiler string and the features are regenerated by `write`).
* 2026-11-19: step 3 implemented (instantiation from the read form). Points the code needed:
  (1) `Compiled.module` holds the inventory's keys only (the bodies stay with the front end's
  module), so tools that need bodies take them from the `ValidModule`. (2) `Compiled.subtype` is
  `heap and subtype_sites`, the instance's old condition, so the written bit never has
  `subtype` without `heap`. (3) The front end's validator still checks a constant expression by
  recursion (`validate._const`): a 300 000-deep `i32.add` chain is refused there with a
  `RecursionError` before compilation; the writer, the reader and the instance do not recurse
  (`tests/artifact/test_instantiate.py` puts such a tree into a compiled module). The validator is
  outside this spec. (4) Under `WABOROS_JIT_ARTIFACT=1` the file's `module_sha256` is the SHA-256 of
  the code (the target does not see the wasm); a test switch only.
* 2026-11-19: the front end's validator no longer recurses on a constant expression
  (`validate._const` uses an explicit stack, same errors in the same order), so point (3) above is
  closed: a 300 000-deep `i32.add` chain in a real binary validates and `jit-x86_64` instantiates it,
  direct and through the file (`tests/test_deep_const.py`). `pyc` codegen still recurses on it.
* 2026-11-20: a constant expression is held as a flat tuple of nodes in postfix order, the order of
  the wasm binary and of the file's `cexpr`, nowhere as a nested structure (LESSONS 136: freeing a
  deep tuple tree crashes CPython 3.13 in a small stack, and Pyodide). Leaves as before
  (`("i32", v)`, `("global", i)`, ...); `("add" | "sub" | "mul", "i32" | "i64")` pops two values;
  `("gc", ins, n)` pops `n`; a non-constant expression is `(("bad",),)`. The writer is a loop over
  the nodes, the reader produces the same flat form, `JitInstance._const` and `pyc`'s `ModGen.val`
  are loops over a value stack. The artifact bytes did not change: `artifact.write` of all 2 525
  spec modules that compile gives the same SHA-256 before and after, and read-then-write gives the
  same bytes (reviewer's comparison; `tools/jit_dump.py` lists, which hash code and tables, not the
  constant expressions, also identical). The validator now reports the first failing node in binary
  order, as Wasmtime does; the old tree walk reported a left operand's type mismatch before an
  unknown global or function on its right (the 2026-11-19 entry's "same errors in the same order"
  no longer holds; `tests/test_validate.py::test_constant_expression_first_error`). A 300 000-deep `i32.add` chain now parses, validates and
  instantiates on `pyc` and `jit-x86_64` (direct and through the file) inside a 256 KiB thread stack
  (`tests/test_deep_const.py`). The earlier "tree" wording in steps 2 and 3 above describes the form
  before this entry.
* 2026-11-20: step 4.1, the rules as implemented. `<version>` of the compiler string
  (`artifact.compiler_string`): the package version verbatim when it is a release; when it contains
  a local part (`+...`), a `.dev`, or is the fallback `0.0.0` of an uninstalled tree, it is the
  version without its local part, `-`, and the first 16 hex digits of SHA-256 over
  `src/waboros/frontend/**/*.py` and `src/waboros/targets/jit/**/*.py` (sorted by path relative to
  the package directory, each as path, NUL, bytes), computed once per process; so a source edit
  changes the string. `load` order: not ours (refused even fat) -> a fat file's wasm hashed against
  `module_sha256` (mismatch: refused, "corrupt", even if everything else is wrong too) -> the
  container and the header (order steps 3-4, `_head`) -> the compatibility checks on the header's
  fields (step 5) -> `read`, the payloads (step 6). The checks: `e_machine` is the running architecture -> `ctx_abi_version` 1 -> `compiler` equals this
  process's string for the loader's target -> `isa_level` 1 -> `isa_features` 0 -> `flags` known
  and `BOUNDS_GUARD` with `GUARD_LOG2` set together -> `BOUNDS_GUARD`: `GUARD_LOG2` at most the
  host's, the target has a guard mode, `guard.install` succeeds, `guard.broken()` is None ->
  `features_required` 0. `features_optional` is accepted. A fat file whose wasm is findable (through
  the ELF header, the section header table and the names only) and fails any of `read`, these
  checks, is compiled by this process with its own options (the normal `compile`; the bounds mode
  is the process's, not the file's). `isa_level` 3 is refused as unknown (this loader runs level 1
  only); a level above 1 takes a registry row when one is written.
  The `e_machine` check also refuses where the host's target cannot run in this process
  (`foreign()`: a 32-bit Python on a 64-bit kernel). A fat file is regenerated for any `Exception`
  in steps 2-6 (a `MemoryError` from the handler's installation included); a thin file's
  unexpected non-`ArtifactError` is wrapped as `ArtifactError` with the cause; a regeneration that
  fails (a wasm whose hash matches but does not parse, validate or compile) raises `ArtifactError`
  chained from the cause. `load` raises `ArtifactError` and nothing else for a bad file.
* 2026-11-20: step 4 is split into 4.1 (loader rules), 4.2 (the GC heap's lazy commit) and 4.3
  (the committed v1 files). 4.3 moves after step 5 because step 5 adds a required section
  (`.waboros.trampolines`), so a v1 file committed earlier would be refused by the final reader
  and would have to be regenerated.
* Step 5 done (2026-11-21): every artifact carries the trampolines of every convention of its
  architecture (`sysv` and `win64` on x86-64, `aapcs64` on aarch64) at the end of `.text`, the
  `.waboros.trampolines` payload (now required) and their symbols; `read` requires every
  convention's record and blob to be `abi.assemble`'s (Changes, 2026-11-21). The targets give
  `conventions()` and `convention` (the running one), from which `trampolines()` seals the
  process's copy. `tests/artifact/test_trampolines.py`. CoreMark files grow by 1 208 bytes
  (x86-64) and 680-688 (aarch64) ([BENCHMARKS](../BENCHMARKS.md), "Artifact size"). Code bytes and
  the D35 lists are unchanged.
* 2026-11-20: step 4.2 done (D69, superseding D57's "committed from its start"; no format, context
  word or code change, so no version bump). `Reservation.commit_range`; `GcHeap` commits the
  header page and the type area as records are written, the whole 4 MiB tid map with the first
  function-type record, and the semispaces, apart; `size_bytes` is the highest committed end. A
  store with one struct commits 2.1 MiB instead of 66 MiB; the 20 GC-using spec files of `gc/`
  and `exceptions/` 43 456 KiB instead of 1 282 112 (BENCHMARKS.md, "GC heap: committed bytes").
  Spec suite 64 229 pass, 0 fail; `gc` and `exceptions` 905 pass with stress, poison, verify and
  the walk check in both bounds modes; D35 lists unchanged on both JITs. Left: 4.3.
* 2026-11-21: step 5 implemented. Points the code needed and the maintainer's answers (2026-11-21):
  (1) **The padding before the first blob is part of the data area** (the `.text` paragraph said
  "exactly the bytes the backend's `finish` returns" while `.waboros.funcs` and
  `.waboros.trampolines` put the first blob at the end of the data area, its zero padding
  included; about three quarters of x86-64 modules have code whose length is not a multiple of
  16). The writer pads the code to 16 and uses that everywhere: `.text`, the aarch64 `$d` symbol
  (emitted when the padded area is not empty) and the read view's `code`, which is the compiled
  code plus 0-15 zero bytes. (2) `saved` is a stack displacement, not a `.text` offset: compared as
  is, never range-checked. (3) The compare covers the whole record relative to `start` (`size`,
  the six offsets, `saved`) and the blob, not only `entry_ret`, `restore` and `saved`. (4) The
  waboros runtime compares every convention in `read`, a foreign host the running one
  ("`.waboros.trampolines`", the two rules); so a flip in any blob is refused (thin) or
  regenerated (fat) and every accepted file writes back to its own bytes. (5) The store rule's
  test runs both instances of a store on a copy sealed from the first file's blob, the only place
  a file's copy is executed. No version bump: the section is in the approved v1 table and no v1
  file has been committed (step 4.3 follows). `.symtab`'s `sh_info` is the first global (the
  entry symbols).
* 2026-11-21: step 4.3 done: the committed v1 files and `tests/artifact/test_old.py`; no change
  to the format or to `src/`.
* 2026-11-21: D70 (one boundary ABI per architecture): steps 6-8 and the v1 freeze wait for
  S0015 (Progress); no change to this spec's text yet.
* 2026-11-21: S0015 revised: the Progress entry now says the step-4.3 files stay as pre-S0015
  fixtures and new v1 files are added, and the exact compiler string and `read`'s trampoline
  compare hold until phase 6; a note under "`.waboros.trampolines`" points to the layout S0015
  replaces it with.
* 2026-11-22: "One copy per store": a copy is never shared between compiler strings, even with
  identical bytes (D71: `HOST_BRIDGE` identity means the same compiler).
* 2026-11-23: S0015 step 4 done: `.waboros.trampolines` is the 28-byte compiler-blob record (bridge,
  `tail_switch`, `restore`, the two restore numbers); the entry and `sp_stub` leave the file (the
  runtime's `target.runtime()`); `.text` carries bridge and `tail_switch` only; the symbols are all
  local, with `__waboros_centry_<conv>` added and `__waboros_entry_<conv>` and the `sp_stub` symbol
  gone, `sh_info` the symbol count; `read` refuses the 40-byte layout on the payload's size and keeps
  the compare against the process's own blobs; "at least 18 words" no longer comes from the entry.
* 2026-11-23: S0015 step 5 (docs): marker notes of D70 on the `compiler` field's D64 clause, "One
  copy per store", the compatibility check of "Semantics: the loader" (the compiler string until
  phase 6), "Alternatives considered" and Q2 (`entry` partly reversed); the test plan names the
  S0015 tests. No format change.
* 2026-11-23: step 7 (docs): Test plan notes on what was built for "the spec suite through files" and
  "Determinism" (`tools/artifact_dump.py`, the `platforms.yml` jobs); D73 added to `related`.
* 2026-11-23: step 8 review findings closed in the reader (no change to what the writer emits):
  a supertype chain deeper than 63, a resume offset not below `data_start`, duplicate or unsorted
  stack-map sites, an empty or over-long compiler-string token, an allocating constant expression
  with `heap` 0, limits the validator refuses (`min` > `max`, memory above 65536 / 2^48 pages,
  table above 2^32-1 / 2^64-1), and a signature word that is not a function type are refused;
  the function-record membership test is a set. The typing rules of constant expressions, the
  container rules and the "Known limitations of v1" are in the normative sections (above).
* 2026-11-24: ELF64 little-endian stays the container for every target, 32-bit ones included (maintainer); `isa_level` numbers disjoint per width where an `e_machine` covers both.
* 2026-11-24 (maintainer): convention ids of `.waboros.trampolines` are append-only and valid per
  `e_machine`; a future architecture's conventions take their own ids under its `e_machine`.
* 2026-11-24 (S0016, maintainer, under the working freeze): the "none" value of a trampoline
  record's offsets leaves v1. Every offset lies inside its blob; `read` refuses `0xFFFFFFFF` by
  name, tested on x86-64 and aarch64 (`tests/artifact/test_trampolines.py`). No file written
  before carried it, so no version bump.
* 2026-11-24 (S0016 step 8): a fat file is compiled with the target passed to `load()`, not "the
  store's compiler" (a store holds several compilers since S0016); marked in "Fat files". No
  format change.
