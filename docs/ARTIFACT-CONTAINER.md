---
title: The artifact container - our own format or ELF (as Wasmtime does)
kind: reference
status: draft
updated: 2026-11-03
---

# The artifact container: our own format or an ELF container

A question from the maintainer, for the orchestrator. It belongs to the phase-5 spec that fixes the
artifact format (D36). **Nothing is decided here.** [ARTIFACT.md](ARTIFACT.md) part 2 describes our
own container (`WABOROS\0`, `u16` section ids) and does not discuss ELF at all; the choice should be
made, with its reasons recorded, **before** the format is fixed. After that, switching containers
means a `format_version` bump, plus changes to the C reference loader and to every micro-compiler
(they emit this format).

## Two different meanings of "the artifact is an ELF"

1. **A native shared library loaded by the OS** (`.so` through `dlopen`, `ctypes.CDLL`). **Rejected
   by our own constraints**, listed so that it is not proposed again:
   * the OS container differs per OS (ELF, Mach-O, PE/COFF), while D23 gives one artifact for every
     OS, because the loader is ours;
   * Android 10+ does not let apps execute files from their data directory and iOS does not allow
     it at all. Our anonymous `mmap` path is what passes in the Android app process (RISKS R2, CI
     the Android cell of `platforms.yml`);
   * a file on disk (temp dirs, `noexec` mounts, cleanup), dynamic linking against runtime symbols,
     the OS loader's policies.
2. **ELF as a container only**, parsed by our own loader (Python and the C reference loader),
   loaded into our own `mmap` memory exactly as today. **This is the real option**, and it has a
   precedent.

## The precedent: Wasmtime's `.cwasm` is an ELF on every OS

Checked here (wasmtime-py, `Module.serialize()` of `poc/in/core.wasm`, `readelf -hS`): a 53 608-byte
**ELF64 relocatable** (`ET_REL`) for x86-64, with a private OS/ABI byte (`0xc8`) so that no OS takes it
for a native object, and these sections:

```
.wasmtime.engine  .wasmtime.bti  .text (AX, 4096-aligned)  .eh_frame  .wasmtime.addrmap
.wasmtime.traps   .wasmtime.exceptions  .rodata.wasm (4096-aligned)  .wasmtime.info
.symtab  .strtab  .shstrtab
```

The SIMD slowdown investigation already used this: Wasmtime's disassembly came "from the ELF of
`Module.serialize()`", with no extra tooling.

**Every target, checked** (wasmtime CLI 50.0.1, `wasmtime compile --target T core.wasm`, raw ELF
identification fields):

| `--target` | class | data | `e_machine` | `e_flags` |
|---|---|---|---|---|
| `x86_64-unknown-linux-gnu`, `x86_64-apple-darwin`, `x86_64-pc-windows-msvc` | ELF64 | LE | 62 `EM_X86_64` | 0x1 |
| `aarch64-apple-darwin` | ELF64 | LE | 183 `EM_AARCH64` | 0x1 |
| `riscv64gc-unknown-linux-gnu` | ELF64 | LE | 243 `EM_RISCV` | 0x1 |
| `s390x-unknown-linux-gnu` | ELF64 | **BE** | 22 `EM_S390` | 0x1 |
| `pulley64` / `pulley64be` (portable bytecode) | ELF64 | LE / BE | 243 (borrowed) | 0x9 |
| `pulley32` / `pulley32be` (32-bit hosts) | **ELF64** | LE / BE | 243 (borrowed) | 0x5 |
| `i686-unknown-linux-gnu`, `armv7-unknown-linux-gnueabihf` | — | — | — | "Support for this target has not been implemented yet" |

Every file is `ET_REL` with OS/ABI `0xc8`, on Linux, macOS and Windows alike. What it shows:

* **Wasmtime never writes ELF32.** It has no native 32-bit code generator; 32-bit hosts run Pulley,
  its portable bytecode interpreter, and that bytecode still sits in an ELF64 file with a borrowed
  `e_machine` and `e_flags` telling pulley32 from pulley64. That works because nothing can
  disassemble Pulley bytecode anyway.
* **The byte order follows the target** (s390x and the `be` Pulley variants are big-endian ELF).
  Our ARTIFACT.md says "a little-endian binary container" for every target; with ELF, a big-endian
  target would make the container big-endian unless we fix `EI_DATA` to LE.
* **Wasmtime's artifacts differ per OS** (`x86_64-unknown-linux-gnu`, `-apple-darwin` and
  `-pc-windows-msvc` give three different files, first difference at byte 69), while our D23 gives
  one artifact for every OS (both entry trampolines inside). The container is the same idea; the
  per-OS split is Wasmtime's, not ELF's.

## What our sections would become

| ARTIFACT.md part 2 | ELF container |
|---|---|
| header: `magic`, `format_version`, `ctx_abi_version`, `isa_level`, `isa_features`, `flags`, `compiler`, `module_sha256`, `config_sha256` | ELF header (`e_machine` = `EM_X86_64` / `EM_AARCH64` / `EM_RISCV` replaces our `arch`; a private `EI_OSABI`) + a `.waboros.header` section with the remaining fields |
| 1 `CODE` | `.text` (`SHF_ALLOC | SHF_EXECINSTR`, page-aligned) |
| 2 `ENTRIES` | symbols `__waboros_entry_sysv`, `__waboros_entry_win64`, … in `.symtab` |
| 3 `FUNCS` | `.symtab` entries (`STT_FUNC`, offset and size; names from the name section or `wasm[N]`) + `.waboros.funcs` for type index and frame size |
| 4-11 `TYPES` … `START` | `.waboros.types`, `.waboros.imports`, … the same payloads, one section each |
| 12 `TRAPMAP` | `.waboros.traps` |
| 13 `RELOCS` | real `SHT_RELA` sections if copy-and-patch ever needs relocations |
| 14 `WASM` (fat file) | `.waboros.wasm` |
| `CHECKSUM` | `.waboros.checksum` (sha256 of the file with this section's bytes zeroed), or dropped in favour of `module_sha256` + `config_sha256` |

The payloads stay as they are; only the framing changes. "Unknown sections are skipped" is ELF's
normal behaviour.

## What it gives

* **Standard tools on every artifact, with no tooling of our own**: `objdump -d`, `readelf`, `nm`
  with real function names; `gdb` and `perf` can use the symbol table once the code is mapped.
  Today we need `WABOROS_JIT_DUMP` plus `objdump` on raw blobs (`isa_all.py`, the SIMD
  investigation).
* **Easier for other-language hosts (D36)**: an ELF reader exists in every language, and the format
  is documented elsewhere. Our own document then only defines the `.waboros.*` payloads.
* **The micro-compilers** emit a format that every toolchain can inspect, which helps when a byte
  difference against the Python oracle has to be understood.
* **Familiar to reviewers**: section flags (`AX`), alignment and symbols say what each part is.

## What it costs

* An ELF64 writer and reader for a fixed set of sections and one symbol table: about 200-300 lines,
  standard library only (`struct`). The C reference loader reads it with a few dozen lines more than
  today.
* Slightly larger files (section headers, string tables).
* **Determinism (D35) must be kept**: a fixed section order, no timestamps, deterministic symbol and
  string table order, so cross-compiled artifacts stay byte-identical. ELF allows this; it has to be a
  rule and a test.
* **The ELF class.** ELF64 for x86-64, aarch64 and riscv64: the tools read `EM_X86_64` in
  ELFCLASS32 as the x32 ABI and aarch64 ELF32 as ILP32, so a 32-bit class there would mislead them.
  *History (D48: no 32-bit JIT is planned, so this is moot).* For the 32-bit architectures (i686,
  armv7, mips32el) there were two options:
  (1) ELF32 with `EM_386` / `EM_ARM` / `EM_MIPS`, so that `objdump` disassembles them as native code
  (the loader then reads both classes, a few dozen lines); (2) ELF64 everywhere, as Wasmtime does
  even for `pulley32` (above) - one reader, but GNU tools may refuse a 32-bit `e_machine` in an
  ELF64 file, which defeats the point of using ELF. Our 32-bit code is native, so (1) fits better;
  decide with the 32-bit design (none is planned).
* **Byte order**: fix `EI_DATA` to little-endian for every target (as ARTIFACT.md's container is
  today), or follow the target as Wasmtime does. It matters only for a big-endian target (s390x; it runs `pyc`, D48, so
  this is moot unless that is reopened).
* `pyc` and `mpy` artifacts (ARTIFACT.md, "Other targets") would also live in the container, with an
  `e_machine` of `EM_NONE` and their own `.waboros.*` code section. That is a little odd but harmless;
  alternatively, keep the current container for them.

## What does not change

* Loading: still our own loader, our own `mmap`, W^X switching, no OS loader, no file needed.
* Thin and fat files (D18), the header fields, the context ABI (part 3), the ISA policy (part 1).
* The rule that a loader refuses what it does not know and never "tries".

## Suggested question for the spec

Q: container of format v1. (a) our own (as written), (b) ELF64 `ET_REL` with a private `EI_OSABI` and
`.waboros.*` sections (Wasmtime's model), (c) our own format plus an export tool that writes an ELF
for inspection only. (c) gets the tooling without a contract change, but then the shipped artifact
and the inspected one are two different files, which is weaker for D36 and the micro-compilers.

## Also open: the file extension

D18 names serialized files `<name>.<target>.<ext>` with `.w2a`, "a working extension until the
project has a name". The project is now waboros (D40), so the extension is decided together with the
container: e.g. `.wab` for our own container, or an ELF-flavoured name if (b) is chosen. The header,
not the name, stays authoritative; the name carries what decides whether a file can run (target, ISA
level, and the bounds mode: guard or explicit, S0006).
