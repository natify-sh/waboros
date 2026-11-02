---
title: Shipping pre-compiled micro-compilers - bootstrap becomes a fallback and a check
kind: reference
status: draft
updated: 2026-11-02
---

# Shipping pre-compiled micro-compilers: bootstrap as a fallback, not a step

A note from the maintainer's discussion, for the orchestrator. It extends
[MICRO-COMPILERS.md](MICRO-COMPILERS.md) ("Bootstrap paths"). **Nothing here is approved for
implementation**: it belongs to the micro-compiler spec, after phase 5 fixes the artifact format
(D36).

## The idea

A micro-compiler compiled to native code is an **ordinary JIT artifact** ([ARTIFACT.md](ARTIFACT.md)).
By D23 an artifact is tied to an architecture and an ISA level, **not to an operating system**: the
same file runs on Linux, macOS, Windows, the BSDs and Android, because our own loader maps it and
both entry trampolines are inside. So the wheel can carry, as plain data files, one pre-compiled
micro-compiler per architecture next to its `.wasm`:

```
waboros/compilers/
  x64.wasm      x64.jit-x86_64-v1.<ext>
  arm64.wasm    arm64.jit-aarch64-v1.<ext>
  riscv64.wasm  riscv64.jit-riscv64-v1.<ext>
```

The wheel stays a single `py3-none-any`. A user on a covered architecture gets a native compiler at
once: **no bootstrap** (no run of the micro-compiler on `pyc`, ~150x slower than native, once per
machine).

## Compared with shipping native binaries the usual way

`meld-cli` on PyPI (0.66.0, checked 2026-11-02) is the usual way: six platform wheels of native
executables (macOS x86_64/arm64, manylinux and musllinux x86_64/aarch64, ~2 MB each), no Windows
wheel, nothing for other architectures. Every OS x architecture pair is a separate build and wheel.

| | native executables (`meld-cli`) | pre-compiled micro-compilers |
|---|---|---|
| unit | one wheel per OS x architecture | one file per architecture (and ISA level), every OS |
| loaded by | the OS loader | our `mmap` loader, as any JIT artifact |
| wheel | platform wheels | one `py3-none-any`, files are data |
| platforms not covered | nothing | bootstrap from the shipped `.wasm` |
| needs a file on disk to execute | yes | no (anonymous memory; works in Android app processes, RISKS R2) |

## Bootstrap becomes the fallback, as fat files are (D18)

Order at first use, per architecture:

1. a **cached** artifact (built earlier on this machine);
2. the **shipped** artifact, if its `arch`, `isa_level`, `format_version` and `ctx_abi_version`
   match what the loader supports;
3. otherwise **bootstrap**: run the shipped `.wasm` on the best available target (`pyc`, or the
   Python JIT on x86-64/aarch64), let it compile itself, cache the result.

The `.wasm` is always shipped, so the package never depends on the pre-compiled files being usable:
they are a cache seeded at build time. On x86-64 and aarch64 the Python JIT can also compile the
`.wasm` directly (the "short" path of MICRO-COMPILERS.md).

## The condition: reproducible, byte for byte

Shipping machine code is acceptable only if anyone can check it. Rule (D35 already requires the
determinism): **a shipped artifact is byte-identical to the artifact a user's own bootstrap
produces** from the shipped `.wasm`, for the same waboros version, architecture and ISA level.

* CI builds every shipped artifact by cross-compilation on one x86-64 runner (D35), then on each
  native runner (x86-64, arm64, riscv64 under QEMU) runs the bootstrap from the `.wasm` and compares
  the result with the shipped file. A difference fails the release.
* The `.wasm` itself comes from a pinned toolchain with a reproducible build (MICRO-COMPILERS.md,
  "Requirements", item 4).
* A user can repeat the check with one command (a `waboros` CLI subcommand later, phase 6): bootstrap
  locally and compare hashes.

So the bootstrap turns from a mandatory installation step into a **verification anyone can run**,
and into the path for architectures nobody pre-compiled.

## What to say honestly

"No native code" stops being literally true once pre-compiled artifacts ship. The accurate statement:
**no native code loaded by the operating system; the machine code we ship is reproducible from the
shipped `.wasm` byte for byte, and the package works without it.** This deserves a decision record
when micro-compilers land (amending the "no native dependency" wording of BRIEF.md, not the rule:
the package still imports only the standard library and still runs where the files are absent).

## Size

Roughly the core plus one backend per file (MICRO-COMPILERS.md estimates ~3-3.5k lines of
Zig/C/Rust per micro-compiler): a few hundred KB of `.wasm` and of native code per architecture, to be
measured. Three architectures add in the order of 1-2 MB uncompressed to the wheel. If that matters,
the pre-compiled files can move to an optional extra (`waboros[compilers]`) while the `.wasm` stays
in the main wheel.

## Bounds-check mode of the shipped files

A micro-compiler takes the bounds mode (explicit checks or guard pages, S0006) as an input, like the
ISA level; which one an artifact uses is the host's choice, because guard code relies on the host's
reservation and fault handler (D29, D44). The **shipped pre-compiled micro-compilers use explicit
checks**: they then run on any host, including other-language hosts (D36) and hosts that may not
install a signal handler, with no contract beyond the loader. A host that supports guard pages may
compile a guard variant for itself. A new architecture's micro-compiler starts with explicit checks
only (D29).

## Open questions for the spec

1. Which ISA levels are shipped pre-compiled: the baseline only (one file per architecture), or
   also v2/v3 variants (D42)? Baseline only is the default; variants are compiled locally on demand.
2. Main wheel or an optional extra for the pre-compiled files.
3. Where the cache lives and how it is keyed (waboros version, architecture, ISA level, format
   version), shared with the module cache of phase 6.
4. Whether the Python compilers stay the default on x86-64/aarch64 once a native micro-compiler for
   them ships (MICRO-COMPILERS.md: only after arm64 and riscv64 have shown the cost of two
   implementations).
