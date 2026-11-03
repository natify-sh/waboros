---
title: Shipping pre-compiled micro-compilers - bootstrap becomes a fallback and a check
kind: reference
status: draft
updated: 2026-11-03
---

# Shipping pre-compiled micro-compilers: bootstrap as a fallback, not a step

A note from the maintainer's discussion, for the orchestrator. It extends
[MICRO-COMPILERS.md](MICRO-COMPILERS.md) ("Bootstrap paths"). **Nothing here is approved for
implementation**: it belongs to the micro-compiler spec, after phase 5 fixes the artifact format
(D36).

## The idea

A micro-compiler compiled to native code is an **ordinary JIT artifact** ([ARTIFACT.md](ARTIFACT.md)).
By D23 an artifact is tied to an architecture and an ISA level, **not to an operating system**: the
same file runs on Linux, macOS, Windows and Android (the BSDs are not a target now), because our own loader maps it and
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

## The toolchain can be wasm too (checked by the maintainer)

A micro-compiler module is built for `wasm32-unknown-unknown` (freestanding: `-nostdlib`, its own
allocator, no WASI imports), as the corpus (`poc/bench/corpus/`) and meld-core already are. The
compiler that builds it can itself be a wasm program:

`natify-sh/py-wasmhost`, `examples/wasmclang.py`, runs **clang and lld compiled to WebAssembly**
(the wasm-clang project of Ben Smith, Apache-2.0: clang 31 MB and lld 19 MB of wasm, LLVM 8.0.1,
WASI `wasi_unstable`, with `memfs.wasm` as an in-memory file system) and builds, then runs:

1. a **freestanding** C module (no libc, no WASI; `--no-entry --allow-undefined --export=...`),
   which is exactly the shape of a micro-compiler;
2. a WASI C program against the sysroot's libc;
3. a C++ program with libc++.

Recorded runs (py-wasmhost README and BACKLOG): Wasmtime, wasm3 and Node; on an iPhone in Pythonista
through `jscontext` (22 s, the C++ link 16 s of it); it aborts on Bun 1.4.2 (a Bun bug). Not in CI.

So the whole chain is wasm, from source to native compiler:

```
clang.wasm + wasm-ld.wasm   (one file each, the same bytes on every host)
  -> x64.wasm / arm64.wasm / riscv64.wasm   (wasm32-unknown-unknown, no WASI)
     -> the micro-compiler compiles itself -> a native artifact
        -> compiles everything else
```

What it gives:

* **Requirement 4 of MICRO-COMPILERS.md in its strongest form**: the toolchain is a file with a
  fixed sha256 and gives the same bytes on Linux, macOS, Windows, Pyodide or a phone; the host's
  system compiler and libc stop mattering.
* **No native binary to trust** between the source and the native compiler (the idea of Diverse
  Double-Compiling and bootstrappable builds, with wasm as the base).
* Eventually the toolchain runs on waboros itself, once WASI is bound (fusedwasi supports
  `wasi_unstable` too, `fusedwasi.p1.wasi_p1_unstable`; its in-memory file system,
  `State(filesystem=...)`, can take memfs's place).

Points to settle:

* **The LLVM version.** wasm-clang is LLVM 8 (2019). Its `wasm32` output is close to MVP, which
  suits requirement 2 (only features every waboros target runs), but its `-Oz` is older than the
  clang 18 that builds the corpus, so modules may be larger. A newer clang built to wasm (with LLD)
  would have to be produced and pinned; none was found on PyPI (`yowasp-clang`, `yowasp-llvm`: 404 on
  2026-11-02).
* **Determinism of clang itself**: the same flags, no absolute paths (`-ffile-prefix-map`, no debug
  info or a fixed `-fdebug-prefix-map`), `SOURCE_DATE_EPOCH`. A CI check: the wasm clang and a
  native clang of the **same LLVM version** build the same micro-compiler sources to byte-identical
  `.wasm`.
* **Speed**: tens of MB of wasm to compile first. On the JIT this is seconds to a minute (Wasmtime
  and `jscontext` numbers above); on `pyc` it is impractical. Running the toolchain is a
  release/CI step or a one-off, not something a user's first import does.

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
